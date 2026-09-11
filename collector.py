# ================================================================
# BLAZE COLLECTOR V2.2 — SOCKET.IO -> NEON
# ================================================================
# Collector-only:
# - Conecta no Socket.IO da Blaze
# - Escuta double.tick
# - Salva somente resultados completos
# - Usa DATABASE_URL do Render/Neon
# - Reconecta automaticamente
# ================================================================

import os
import threading
import time
from datetime import datetime

import psycopg2
import socketio


BLAZE_URL = os.getenv(
    "BLAZE_URL",
    "https://api-gaming.blaze.bet.br"
)

SOCKET_PATH = os.getenv(
    "SOCKET_PATH",
    "/replication/"
)

ROOM = os.getenv(
    "BLAZE_ROOM",
    "double_room_1"
)

EVENT_NAME = "data"
TICK_NAME = "double.tick"


estado = {
    "rodando": False,
    "conectado": False,
    "total_ticks": 0,
    "total_resultados": 0,
    "total_duplicados": 0,
    "total_erros_db": 0,
    "ultima_rodada": None,
}

estado_lock = threading.Lock()
thread_collector = None
sio = None


# ================================================================
# BANCO
# ================================================================

def get_db_connection():
    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        raise RuntimeError("DATABASE_URL não configurada.")

    if "sslmode=" not in database_url:
        separator = "&" if "?" in database_url else "?"
        database_url = f"{database_url}{separator}sslmode=require"

    return psycopg2.connect(
        database_url,
        connect_timeout=10,
    )


def testar_postgresql():
    conn = get_db_connection()

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1;")
            cur.fetchone()

        conn.close()
        print("✅ Neon PostgreSQL conectado.")
        return True

    except Exception as e:
        print(f"❌ Erro PostgreSQL: {e}")
        try:
            conn.close()
        except Exception:
            pass
        return False


def init_db():
    conn = get_db_connection()

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
                INSERT INTO collector_status (id)
                VALUES (1)
                ON CONFLICT (id) DO NOTHING;
            """)

        conn.commit()
        conn.close()

    except Exception:
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass
        raise


def atualizar_status_db(conectado=None, ultima_rodada=None,
                         resultado_novo=False, duplicado=False,
                         erro_db=False, tick=False):
    try:
        conn = get_db_connection()

        with conn.cursor() as cur:
            sets = [
                "atualizado_em = CURRENT_TIMESTAMP"
            ]
            values = []

            if conectado is not None:
                sets.append("conectado = %s")
                values.append(conectado)

            if ultima_rodada is not None:
                sets.append("ultima_rodada = %s")
                values.append(str(ultima_rodada))

            if resultado_novo:
                sets.append("total_resultados = total_resultados + 1")
                sets.append("ultimo_resultado_em = CURRENT_TIMESTAMP")

            if duplicado:
                sets.append("total_duplicados = total_duplicados + 1")

            if erro_db:
                sets.append("total_erros_db = total_erros_db + 1")

            if tick:
                sets.append("total_ticks = total_ticks + 1")

            query = f"""
                UPDATE collector_status
                SET {", ".join(sets)}
                WHERE id = 1;
            """

            cur.execute(query, values)

        conn.commit()
        conn.close()

    except Exception as e:
        print(f"⚠️ Erro ao atualizar status: {e}")


# ================================================================
# NORMALIZAÇÃO
# ================================================================

def normalizar_cor(color):
    try:
        color = int(color)
    except Exception:
        return None

    if color == 0:
        return "BRANCO"
    if color == 1:
        return "VERMELHO"
    if color == 2:
        return "PRETO"

    return None


def extrair_valor(payload, *chaves):
    for chave in chaves:
        if isinstance(payload, dict) and chave in payload:
            return payload[chave]
    return None


# ================================================================
# SALVAR RESULTADO
# ================================================================

def salvar_resultado(payload):
    if not isinstance(payload, dict):
        return False

    rodada_id = extrair_valor(
        payload,
        "id",
        "round_id",
        "roundId",
        "rodada_id",
        "game_id",
        "gameId",
    )

    color = extrair_valor(
        payload,
        "color",
        "colour",
    )

    roll = extrair_valor(
        payload,
        "roll",
        "number",
    )

    status = extrair_valor(payload, "status")
    room_id = extrair_valor(
        payload,
        "room_id",
        "roomId",
    ) or ROOM

    created_at = extrair_valor(
        payload,
        "created_at",
        "createdAt",
    )

    updated_at = extrair_valor(
        payload,
        "updated_at",
        "updatedAt",
    )

    if status is not None and str(status).lower() != "complete":
        return False

    if rodada_id is None or color is None or roll is None:
        return False

    cor = normalizar_cor(color)

    if cor is None:
        return False

    try:
        conn = get_db_connection()

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
                ON CONFLICT (rodada_id) DO NOTHING
                RETURNING id;
            """, (
                str(rodada_id),
                int(color),
                cor,
                int(roll),
                str(status or "complete"),
                str(room_id),
                created_at,
                updated_at,
            ))

            inserted = cur.fetchone()

        conn.commit()
        conn.close()

        if inserted:
            with estado_lock:
                estado["total_resultados"] += 1
                estado["ultima_rodada"] = str(rodada_id)

            atualizar_status_db(
                ultima_rodada=rodada_id,
                resultado_novo=True,
            )

            print(
                f"💾 RESULTADO | rodada={rodada_id} "
                f"| roll={roll} | cor={cor}"
            )

            return True

        with estado_lock:
            estado["total_duplicados"] += 1

        atualizar_status_db(duplicado=True)
        return False

    except Exception as e:
        with estado_lock:
            estado["total_erros_db"] += 1

        print(f"❌ Erro salvando resultado: {e}")
        atualizar_status_db(erro_db=True)
        return False


# ================================================================
# PROCESSAR EVENTO
# ================================================================

def processar_tick(data):
    with estado_lock:
        estado["total_ticks"] += 1

    atualizar_status_db(tick=True)

    if not isinstance(data, dict):
        return

    if data.get("id") != TICK_NAME:
        return

    payload = data.get("payload")

    if not isinstance(payload, dict):
        return

    salvar_resultado(payload)


# ================================================================
# SOCKET.IO
# ================================================================

def configurar_socket():
    global sio

    sio = socketio.Client(
        reconnection=True,
        reconnection_attempts=0,
        reconnection_delay=2,
        reconnection_delay_max=10,
        logger=False,
        engineio_logger=False,
    )

    @sio.event
    def connect():
        with estado_lock:
            estado["conectado"] = True

        print("🟢 SOCKET.IO CONECTADO AO BLAZE")
        print(f"📡 Room: {ROOM}")

        atualizar_status_db(conectado=True)

        # Assinatura usada pelo collector original.
        sio.emit(
            "cmd",
            {
                "room": ROOM
            }
        )

        print("📡 Assinatura enviada.")

    @sio.event
    def disconnect():
        with estado_lock:
            estado["conectado"] = False

        print("🔴 SOCKET.IO DESCONECTADO")
        atualizar_status_db(conectado=False)

    @sio.event
    def connect_error(data):
        with estado_lock:
            estado["conectado"] = False

        print(f"⚠️ SOCKET.IO connect_error: {data}")
        atualizar_status_db(conectado=False)

    @sio.on(EVENT_NAME)
    def on_data(data):
        processar_tick(data)


# ================================================================
# EXECUÇÃO
# ================================================================

def executar_coletor():
    global sio

    with estado_lock:
        estado["rodando"] = True

    print("=" * 70)
    print("🚀 BLAZE COLLECTOR V2.2")
    print("=" * 70)
    print(f"🌐 URL: {BLAZE_URL}")
    print(f"🔌 Socket path: {SOCKET_PATH}")
    print(f"🏠 Room: {ROOM}")
    print("=" * 70)

    while True:
        try:
            if not testar_postgresql():
                time.sleep(10)
                continue

            init_db()

            configurar_socket()

            print("🔌 Conectando ao Socket.IO...")

            sio.connect(
                BLAZE_URL,
                socketio_path=SOCKET_PATH,
                transports=["websocket"],
                wait_timeout=20,
            )

            while True:
                time.sleep(5)

                if sio is None or not sio.connected:
                    print("⚠️ Socket não está conectado. Recomeçando...")
                    break

        except Exception as e:
            with estado_lock:
                estado["conectado"] = False

            print(f"❌ Erro no collector: {e}")
            atualizar_status_db(conectado=False)

            try:
                if sio is not None and sio.connected:
                    sio.disconnect()
            except Exception:
                pass

            time.sleep(5)


def iniciar_coletor_em_thread():
    global thread_collector

    if thread_collector and thread_collector.is_alive():
        return thread_collector

    thread_collector = threading.Thread(
        target=executar_coletor,
        name="blaze-collector",
        daemon=True,
    )

    thread_collector.start()

    return thread_collector


def snapshot_estado():
    with estado_lock:
        return dict(estado)
