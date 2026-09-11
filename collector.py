# ================================================================
# BLAZE DOUBLE — COLLECTOR V2.2
# Render + Neon
# ================================================================
# Objetivo:
#   Capturar resultados finais do Blaze Double via Socket.IO
#   e gravar no PostgreSQL/Neon.
#
# IMPORTANTE:
#   - Não existe DATABASE_URL hardcoded.
#   - A DATABASE_URL vem exclusivamente das Environment Variables
#     do Render.
#   - O coletor não usa a API REST /roulette_games/recent/1.
#   - Somente eventos double.tick com status=complete são salvos.
# ================================================================

import os
import time
import threading
from datetime import datetime

import socketio
import psycopg2


# ================================================================
# CONFIGURAÇÃO
# ================================================================

BLAZE_URL = os.getenv("BLAZE_URL", "https://api-gaming.blaze.bet.br")
SOCKET_PATH = os.getenv("SOCKET_PATH", "/replication/")
ROOM = os.getenv("BLAZE_ROOM", "double_room_1")

EVENT_NAME = "data"
TICK_NAME = "double.tick"

INTERVALO_STATUS = int(os.getenv("COLLECTOR_STATUS_INTERVAL", "30"))
RETRY_DELAY = int(os.getenv("COLLECTOR_RETRY_DELAY", "5"))

MOSTRAR_TICKS = os.getenv("MOSTRAR_TICKS", "false").lower() == "true"


CORES = {
    0: "BRANCO",
    1: "VERMELHO",
    2: "PRETO",
}


# ================================================================
# ESTADO
# ================================================================

rodando = True
conectado = False
sio = None

total_ticks = 0
total_resultados = 0
total_duplicados = 0
total_erros_db = 0

ultima_rodada = None
ultimo_resultado_em = None

estado_lock = threading.Lock()


# ================================================================
# UTILIDADES
# ================================================================

def nome_cor(cor):
    try:
        return CORES.get(int(cor), f"DESCONHECIDA({int(cor)})")
    except Exception:
        return "DESCONHECIDA"


def agora():
    return datetime.utcnow()


# ================================================================
# POSTGRESQL / NEON
# ================================================================

def get_db_connection():
    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        print("❌ DATABASE_URL não configurada no Render.")
        return None

    try:
        if "sslmode=" not in database_url:
            separator = "&" if "?" in database_url else "?"
            database_url = f"{database_url}{separator}sslmode=require"

        return psycopg2.connect(
            database_url,
            connect_timeout=15,
            keepalives=1,
            keepalives_idle=30,
            keepalives_interval=10,
            keepalives_count=5,
        )

    except Exception as e:
        print(f"❌ Erro ao conectar no Neon: {e}")
        return None


def testar_postgresql():
    conn = get_db_connection()

    if not conn:
        return False

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1;")
            cur.fetchone()

        conn.close()
        print("✅ Neon PostgreSQL conectado.")
        return True

    except Exception as e:
        print(f"❌ Falha no teste do Neon: {e}")

        try:
            conn.close()
        except Exception:
            pass

        return False


def init_db():
    conn = get_db_connection()

    if not conn:
        return False

    try:
        with conn.cursor() as cur:

            cur.execute("""
                CREATE TABLE IF NOT EXISTS blaze_historico (
                    id SERIAL PRIMARY KEY,
                    rodada_id VARCHAR(100) UNIQUE NOT NULL,
                    color INTEGER,
                    cor VARCHAR(20),
                    roll INTEGER,
                    status VARCHAR(30),
                    room_id VARCHAR(100),
                    created_at TIMESTAMP,
                    updated_at TIMESTAMP,
                    coletado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_blaze_created_at
                ON blaze_historico(created_at);
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_blaze_cor
                ON blaze_historico(cor);
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_blaze_roll
                ON blaze_historico(roll);
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS collector_status (
                    id INTEGER PRIMARY KEY,
                    conectado BOOLEAN DEFAULT FALSE,
                    ultima_rodada VARCHAR(100),
                    ultimo_resultado_em TIMESTAMP,
                    total_ticks INTEGER DEFAULT 0,
                    total_resultados INTEGER DEFAULT 0,
                    total_duplicados INTEGER DEFAULT 0,
                    total_erros_db INTEGER DEFAULT 0,
                    atualizado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            cur.execute("""
                INSERT INTO collector_status (
                    id,
                    conectado,
                    atualizado_em
                )
                VALUES (1, FALSE, CURRENT_TIMESTAMP)
                ON CONFLICT (id) DO NOTHING;
            """)

        conn.commit()
        conn.close()

        print("✅ Tabelas do coletor prontas.")
        return True

    except Exception as e:
        print(f"❌ Erro ao inicializar banco: {e}")

        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass

        return False


def atualizar_status_db():
    conn = get_db_connection()

    if not conn:
        return

    try:
        with estado_lock:
            _conectado = conectado
            _ultima_rodada = ultima_rodada
            _ultimo_resultado_em = ultimo_resultado_em
            _ticks = total_ticks
            _resultados = total_resultados
            _duplicados = total_duplicados
            _erros = total_erros_db

        with conn.cursor() as cur:
            cur.execute("""
                UPDATE collector_status
                SET
                    conectado = %s,
                    ultima_rodada = %s,
                    ultimo_resultado_em = %s,
                    total_ticks = %s,
                    total_resultados = %s,
                    total_duplicados = %s,
                    total_erros_db = %s,
                    atualizado_em = CURRENT_TIMESTAMP
                WHERE id = 1;
            """, (
                _conectado,
                _ultima_rodada,
                _ultimo_resultado_em,
                _ticks,
                _resultados,
                _duplicados,
                _erros,
            ))

        conn.commit()
        conn.close()

    except Exception as e:
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass

        print(f"⚠️ Não foi possível atualizar status no Neon: {e}")


# ================================================================
# SALVAR RESULTADO
# ================================================================

def salvar_resultado(payload):
    global total_resultados
    global total_duplicados
    global total_erros_db
    global ultima_rodada
    global ultimo_resultado_em

    rodada_id = payload.get("id")
    color = payload.get("color")
    roll = payload.get("roll")
    status = payload.get("status")
    room_id = payload.get("room_id")

    created_at = payload.get("created_at")
    updated_at = payload.get("updated_at")

    if not rodada_id:
        return False

    if color is None or roll is None:
        return False

    if status != "complete":
        return False

    cor_nome = nome_cor(color)

    created_datetime = None
    updated_datetime = None

    try:
        if created_at:
            created_datetime = datetime.fromisoformat(
                str(created_at).replace("Z", "+00:00")
            ).replace(tzinfo=None)
    except Exception:
        pass

    try:
        if updated_at:
            updated_datetime = datetime.fromisoformat(
                str(updated_at).replace("Z", "+00:00")
            ).replace(tzinfo=None)
    except Exception:
        pass

    conn = get_db_connection()

    if not conn:
        with estado_lock:
            total_erros_db += 1
        atualizar_status_db()
        return False

    try:
        with conn.cursor() as cur:

            cur.execute("""
                INSERT INTO blaze_historico (
                    rodada_id,
                    color,
                    cor,
                    roll,
                    status,
                    room_id,
                    created_at,
                    updated_at
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (rodada_id)
                DO NOTHING
                RETURNING id;
            """, (
                str(rodada_id),
                int(color),
                cor_nome,
                int(roll),
                status,
                str(room_id) if room_id is not None else None,
                created_datetime,
                updated_datetime,
            ))

            inserido = cur.fetchone()

        conn.commit()
        conn.close()

        with estado_lock:
            if inserido:
                total_resultados += 1
                ultima_rodada = str(rodada_id)
                ultimo_resultado_em = agora()
            else:
                total_duplicados += 1

        if inserido:
            print(
                f"💾 RESULTADO | rodada={rodada_id} | "
                f"cor={cor_nome} | roll={roll}"
            )

        atualizar_status_db()
        return bool(inserido)

    except Exception as e:
        with estado_lock:
            total_erros_db += 1

        print(f"❌ Erro ao salvar resultado no Neon: {e}")

        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass

        atualizar_status_db()
        return False


# ================================================================
# PROCESSAR DOUBLE.TICK
# ================================================================

def processar_tick(data):
    global total_ticks

    with estado_lock:
        total_ticks += 1

    if not isinstance(data, dict):
        return

    if data.get("id") != TICK_NAME:
        return

    payload = data.get("payload")

    if not isinstance(payload, dict):
        return

    rodada_id = payload.get("id")
    status = payload.get("status")
    color = payload.get("color")
    roll = payload.get("roll")

    if MOSTRAR_TICKS:
        print(
            f"📡 TICK | id={rodada_id} | "
            f"status={status} | "
            f"cor={nome_cor(color) if color is not None else '-'} | "
            f"roll={roll}"
        )

    # Só interessa o resultado final.
    if status != "complete":
        return

    if color is None or roll is None:
        print(f"⚠️ COMPLETE incompleto | rodada={rodada_id}")
        return

    salvar_resultado(payload)


# ================================================================
# SOCKET.IO
# ================================================================

def configurar_socket():
    global sio

    sio = socketio.Client(
        reconnection=True,
        reconnection_attempts=0,       # infinito
        reconnection_delay=2,
        reconnection_delay_max=10,
        logger=False,
        engineio_logger=False,
    )

    @sio.event
    def connect():
        global conectado

        conectado = True

        print()
        print("=" * 70)
        print("🟢 SOCKET.IO CONECTADO AO BLAZE")
        print("=" * 70)
        print(f"Servidor : {BLAZE_URL}")
        print(f"Path     : {SOCKET_PATH}")
        print(f"Room     : {ROOM}")
        print("=" * 70)

        atualizar_status_db()

        try:
            sio.emit(
                "cmd",
                {
                    "id": "subscribe",
                    "payload": {
                        "room": ROOM
                    }
                }
            )

            print(f"📡 Subscribe enviado: {ROOM}")

        except Exception as e:
            print(f"❌ Erro ao enviar subscribe: {e}")


    @sio.event
    def disconnect():
        global conectado

        conectado = False

        print("🔴 Socket.IO desconectado.")
        atualizar_status_db()


    @sio.event
    def connect_error(data):
        global conectado

        conectado = False

        print(f"⚠️ Erro de conexão Socket.IO: {data}")
        atualizar_status_db()


    @sio.on(EVENT_NAME)
    def on_data(data):
        try:
            processar_tick(data)
        except Exception as e:
            print(f"❌ Erro processando evento data: {e}")


# ================================================================
# LOOP DO COLETOR
# ================================================================

def executar_coletor():
    global rodando
    global conectado

    print()
    print("=" * 70)
    print("BLAZE DOUBLE — COLLECTOR V2.2")
    print("RENDER + NEON")
    print("=" * 70)
    print("Modo: Socket.IO")
    print("Banco: PostgreSQL / Neon")
    print(f"Room: {ROOM}")
    print("=" * 70)

    if not os.getenv("DATABASE_URL"):
        print("❌ DATABASE_URL não existe.")
        return

    while rodando:

        try:
            if not testar_postgresql():
                print(f"⏳ Neon indisponível. Nova tentativa em {RETRY_DELAY}s.")
                time.sleep(RETRY_DELAY)
                continue

            if not init_db():
                print(f"⏳ Falha ao inicializar banco. Nova tentativa em {RETRY_DELAY}s.")
                time.sleep(RETRY_DELAY)
                continue

            configurar_socket()

            print("🔌 Tentando conectar ao Socket.IO...")

            sio.connect(
                BLAZE_URL,
                socketio_path=SOCKET_PATH,
                transports=["websocket"],
                wait_timeout=20,
            )

            # Mantém o cliente vivo.
            while rodando and sio.connected:
                time.sleep(1)

        except Exception as e:
            conectado = False
            atualizar_status_db()

            print(f"❌ Falha no coletor: {e}")

            try:
                if sio and sio.connected:
                    sio.disconnect()
            except Exception:
                pass

            if rodando:
                print(f"⏳ Reconectando em {RETRY_DELAY}s...")
                time.sleep(RETRY_DELAY)

        finally:
            conectado = False
            atualizar_status_db()

    try:
        if sio and sio.connected:
            sio.disconnect()
    except Exception:
        pass

    print("🛑 Collector encerrado.")


# ================================================================
# THREAD / STATUS
# ================================================================

def iniciar_coletor_em_thread():
    thread = threading.Thread(
        target=executar_coletor,
        name="blaze-collector",
        daemon=True,
    )
    thread.start()
    return thread


def snapshot_estado():
    with estado_lock:
        return {
            "conectado": conectado,
            "ultima_rodada": ultima_rodada,
            "ultimo_resultado_em": ultimo_resultado_em,
            "total_ticks": total_ticks,
            "total_resultados": total_resultados,
            "total_duplicados": total_duplicados,
            "total_erros_db": total_erros_db,
        }
