import os
import time
import threading
import traceback
from datetime import datetime

import psycopg2
import socketio


# ============================================================
# BLAZE DOUBLE — COLLECTOR PARA RENDER + NEON
# ============================================================

BLAZE_URL = os.getenv("BLAZE_URL", "https://api-gaming.blaze.bet.br")
SOCKET_PATH = os.getenv("SOCKET_PATH", "/replication/")
ROOM = os.getenv("BLAZE_ROOM", "double_room_1")

EVENT_NAME = "data"
TICK_NAME = "double.tick"

DATABASE_URL = os.getenv("DATABASE_URL")

rodando = True
sio = None


# ============================================================
# CORES BLAZE
# ============================================================

CORES = {
    0: "BRANCO",
    1: "VERMELHO",
    2: "PRETO",
}


def nome_cor(color):
    try:
        return CORES.get(int(color), f"DESCONHECIDA({color})")
    except Exception:
        return "DESCONHECIDA"


# ============================================================
# POSTGRESQL
# ============================================================

def get_db_connection():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL não configurada")

    url = DATABASE_URL

    if "sslmode=" not in url:
        separator = "&" if "?" in url else "?"
        url += f"{separator}sslmode=require"

    return psycopg2.connect(
        url,
        connect_timeout=15
    )


# ============================================================
# BANCO — CRIAÇÃO + MIGRAÇÃO SEGURA
# ============================================================

def init_db():
    """
    O Neon já possui tabelas criadas por versões anteriores do projeto.

    CREATE TABLE IF NOT EXISTS NÃO altera uma tabela existente.
    Por isso esta função também adiciona somente as colunas que
    estiverem faltando.

    Isso corrige os erros:
      column "coletado_em" does not exist
      column "status" of relation "collector_status" does not exist
    """

    conn = get_db_connection()

    try:
        with conn.cursor() as cur:

            # ----------------------------------------------------
            # Tabela principal
            # ----------------------------------------------------
            cur.execute("""
                CREATE TABLE IF NOT EXISTS blaze_historico (
                    id SERIAL PRIMARY KEY,
                    rodada_id VARCHAR(100) UNIQUE NOT NULL,
                    color INTEGER,
                    cor VARCHAR(20),
                    roll INTEGER,
                    status VARCHAR(30),
                    room_id INTEGER,
                    created_at TIMESTAMP,
                    updated_at TIMESTAMP,
                    coletado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Migração da tabela já existente
            cur.execute("""
                ALTER TABLE blaze_historico
                ADD COLUMN IF NOT EXISTS cor VARCHAR(20)
            """)

            cur.execute("""
                ALTER TABLE blaze_historico
                ADD COLUMN IF NOT EXISTS status VARCHAR(30)
            """)

            cur.execute("""
                ALTER TABLE blaze_historico
                ADD COLUMN IF NOT EXISTS room_id INTEGER
            """)

            cur.execute("""
                ALTER TABLE blaze_historico
                ADD COLUMN IF NOT EXISTS created_at TIMESTAMP
            """)

            cur.execute("""
                ALTER TABLE blaze_historico
                ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP
            """)

            cur.execute("""
                ALTER TABLE blaze_historico
                ADD COLUMN IF NOT EXISTS coletado_em TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP
            """)

            # Índices
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_blaze_historico_rodada
                ON blaze_historico (rodada_id)
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_blaze_historico_coletado
                ON blaze_historico (coletado_em DESC)
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_blaze_historico_created
                ON blaze_historico (created_at)
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_blaze_historico_cor
                ON blaze_historico (cor)
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_blaze_historico_roll
                ON blaze_historico (roll)
            """)

            # ----------------------------------------------------
            # Tabela de status
            # ----------------------------------------------------
            cur.execute("""
                CREATE TABLE IF NOT EXISTS collector_status (
                    id INTEGER PRIMARY KEY,
                    status TEXT,
                    ultima_atualizacao TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    ultima_rodada TEXT,
                    ultima_cor TEXT,
                    ultimo_roll INTEGER,
                    mensagem TEXT
                )
            """)

            # Migração da tabela já existente
            cur.execute("""
                ALTER TABLE collector_status
                ADD COLUMN IF NOT EXISTS status TEXT
            """)

            cur.execute("""
                ALTER TABLE collector_status
                ADD COLUMN IF NOT EXISTS ultima_atualizacao TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP
            """)

            cur.execute("""
                ALTER TABLE collector_status
                ADD COLUMN IF NOT EXISTS ultima_rodada TEXT
            """)

            cur.execute("""
                ALTER TABLE collector_status
                ADD COLUMN IF NOT EXISTS ultima_cor TEXT
            """)

            cur.execute("""
                ALTER TABLE collector_status
                ADD COLUMN IF NOT EXISTS ultimo_roll INTEGER
            """)

            cur.execute("""
                ALTER TABLE collector_status
                ADD COLUMN IF NOT EXISTS mensagem TEXT
            """)

        conn.commit()

        print("✅ Banco PostgreSQL verificado/migrado", flush=True)

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


# ============================================================
# STATUS DO COLETOR
# ============================================================

def atualizar_status(
    status,
    mensagem=None,
    rodada=None,
    cor=None,
    roll=None
):
    try:
        conn = get_db_connection()

        try:
            with conn.cursor() as cur:

                # Primeiro tenta atualizar a linha ID=1.
                cur.execute("""
                    UPDATE collector_status
                    SET
                        status = %s,
                        ultima_atualizacao = CURRENT_TIMESTAMP,
                        ultima_rodada = COALESCE(%s, ultima_rodada),
                        ultima_cor = COALESCE(%s, ultima_cor),
                        ultimo_roll = COALESCE(%s, ultimo_roll),
                        mensagem = %s
                    WHERE id = 1
                """, (
                    status,
                    rodada,
                    cor,
                    roll,
                    mensagem
                ))

                # Se ainda não existe ID=1, cria.
                if cur.rowcount == 0:
                    cur.execute("""
                        INSERT INTO collector_status (
                            id,
                            status,
                            ultima_atualizacao,
                            ultima_rodada,
                            ultima_cor,
                            ultimo_roll,
                            mensagem
                        )
                        VALUES (
                            1,
                            %s,
                            CURRENT_TIMESTAMP,
                            %s,
                            %s,
                            %s,
                            %s
                        )
                    """, (
                        status,
                        rodada,
                        cor,
                        roll,
                        mensagem
                    ))

            conn.commit()

        finally:
            conn.close()

    except Exception as e:
        print(
            f"⚠️ Erro ao atualizar status do coletor: {e}",
            flush=True
        )


# ============================================================
# CONVERSÃO DE DATA
# ============================================================

def converter_data(valor):
    """
    Converte ISO 8601 para datetime sem timezone porque a tabela
    histórica existente usa TIMESTAMP (sem timezone).
    """

    if not valor:
        return None

    try:
        texto = str(valor).replace("Z", "+00:00")

        dt = datetime.fromisoformat(texto)

        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)

        return dt

    except Exception:
        return None


# ============================================================
# SALVAR RESULTADO
# ============================================================

def salvar_resultado(payload):
    if not isinstance(payload, dict):
        return False

    rodada_id = (
        payload.get("id")
        or payload.get("round_id")
        or payload.get("roundId")
        or payload.get("rodada_id")
        or payload.get("game_id")
        or payload.get("gameId")
    )

    color = (
        payload.get("color")
        if payload.get("color") is not None
        else payload.get("colour")
    )

    roll = (
        payload.get("roll")
        if payload.get("roll") is not None
        else payload.get("number")
    )

    status = payload.get("status")

    room_id = (
        payload.get("room_id")
        if payload.get("room_id") is not None
        else payload.get("roomId")
    )

    created_at = converter_data(
        payload.get("created_at")
        or payload.get("createdAt")
    )

    updated_at = converter_data(
        payload.get("updated_at")
        or payload.get("updatedAt")
    )

    if not rodada_id:
        return False

    if color is None:
        return False

    if roll is None:
        return False

    if str(status).lower() != "complete":
        return False

    # A tabela existente do projeto usa COLOR como INTEGER.
    try:
        color_int = int(color)
    except (TypeError, ValueError):
        print(
            f"⚠️ Resultado ignorado: color inválido: {color}",
            flush=True
        )
        return False

    try:
        roll_int = int(roll)
    except (TypeError, ValueError):
        print(
            f"⚠️ Resultado ignorado: roll inválido: {roll}",
            flush=True
        )
        return False

    # A tabela existente usa ROOM_ID como INTEGER.
    # Se o payload não fornecer um valor numérico, usamos NULL.
    try:
        room_id_int = int(room_id) if room_id is not None else None
    except (TypeError, ValueError):
        room_id_int = None

    rodada_id = str(rodada_id)
    cor_nome = nome_cor(color_int)

    conn = get_db_connection()

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
                    updated_at,
                    coletado_em
                )
                VALUES (
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    CURRENT_TIMESTAMP
                )
                ON CONFLICT (rodada_id)
                DO NOTHING
                RETURNING id
            """, (
                rodada_id,
                color_int,
                cor_nome,
                roll_int,
                str(status),
                room_id_int,
                created_at,
                updated_at
            ))

            resultado = cur.fetchone()

        conn.commit()

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()

    if resultado:
        print("=" * 72, flush=True)
        print("✅ RESULTADO SALVO NO NEON", flush=True)
        print(f"Rodada : {rodada_id}", flush=True)
        print(f"Cor    : {cor_nome}", flush=True)
        print(f"Color  : {color_int}", flush=True)
        print(f"Roll   : {roll_int}", flush=True)
        print(f"Status : {status}", flush=True)
        print("=" * 72, flush=True)

        atualizar_status(
            "COLETANDO",
            "Novo resultado recebido",
            rodada_id,
            cor_nome,
            roll_int
        )

        return True

    return False


# ============================================================
# SOCKET.IO
# ============================================================

def criar_socket():
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
        print(
            "🟢 SOCKET.IO CONECTADO AO BLAZE",
            flush=True
        )

        atualizar_status(
            "CONECTADO",
            "Socket.IO conectado"
        )

    @sio.event
    def disconnect():
        print(
            "🔴 SOCKET.IO DESCONECTADO",
            flush=True
        )

        atualizar_status(
            "DESCONECTADO",
            "Socket.IO desconectado"
        )

    @sio.event
    def connect_error(data):
        print(
            f"⚠️ ERRO DE CONEXÃO SOCKET.IO: {data}",
            flush=True
        )

        atualizar_status(
            "ERRO",
            f"Erro de conexão: {data}"
        )

    @sio.on(EVENT_NAME)
    def on_data(data):
        processar_tick(data)


# ============================================================
# INSCRIÇÃO NA SALA
# ============================================================

def inscrever_room():
    if sio is None:
        return False

    if not sio.connected:
        print(
            "⚠️ Socket não está conectado para inscrição",
            flush=True
        )
        return False

    try:
        print(
            f"📡 ENVIANDO INSCRIÇÃO | room={ROOM}",
            flush=True
        )

        sio.emit(
            "cmd",
            {
                "room": ROOM
            }
        )

        print(
            f"📡 INSCRIÇÃO ENVIADA | room={ROOM}",
            flush=True
        )

        atualizar_status(
            "INSCRITO",
            f"Inscrição enviada para {ROOM}"
        )

        return True

    except Exception as e:
        print(
            f"❌ ERRO AO ENVIAR INSCRIÇÃO: {e}",
            flush=True
        )

        atualizar_status(
            "ERRO",
            f"Erro na inscrição: {e}"
        )

        return False


# ============================================================
# PROCESSAR TICK
# ============================================================

def processar_tick(data):
    if not isinstance(data, dict):
        return

    if data.get("id") != TICK_NAME:
        return

    payload = data.get("payload")

    if not isinstance(payload, dict):
        return

    status = str(
        payload.get("status", "")
    ).lower()

    # Somente resultado final.
    if status != "complete":
        return

    print(
        f"📥 TICK FINAL RECEBIDO | "
        f"id={payload.get('id')} "
        f"color={payload.get('color')} "
        f"roll={payload.get('roll')}",
        flush=True
    )

    try:
        salvar_resultado(payload)

    except Exception as e:
        print(
            f"❌ ERRO AO SALVAR RESULTADO: {e}",
            flush=True
        )
        traceback.print_exc()


# ============================================================
# EXECUÇÃO PRINCIPAL
# ============================================================

def executar_coletor():

    print("=" * 72, flush=True)
    print("🎰 BLAZE COLLECTOR — RENDER + NEON", flush=True)
    print("=" * 72, flush=True)
    print(f"🌐 Blaze       : {BLAZE_URL}", flush=True)
    print(f"🔌 Socket Path : {SOCKET_PATH}", flush=True)
    print(f"🏠 Room        : {ROOM}", flush=True)
    print("=" * 72, flush=True)

    while rodando:

        try:
            # ----------------------------------------------------
            # BANCO
            # ----------------------------------------------------
            print(
                "🔌 Verificando PostgreSQL/Neon...",
                flush=True
            )

            init_db()

            atualizar_status(
                "INICIANDO",
                "Banco verificado"
            )

            print(
                "✅ PostgreSQL/Neon OK",
                flush=True
            )

            # ----------------------------------------------------
            # SOCKET
            # ----------------------------------------------------
            criar_socket()

            print(
                "🔌 Conectando ao Socket.IO da Blaze...",
                flush=True
            )

            atualizar_status(
                "CONECTANDO",
                "Conectando ao Socket.IO"
            )

            sio.connect(
                BLAZE_URL,
                socketio_path=SOCKET_PATH,
                transports=["websocket"],
                wait_timeout=20
            )

            # IMPORTANTE:
            # O emit NÃO fica dentro de connect().
            # Ele ocorre somente depois que sio.connect()
            # retornou e sio.connected foi confirmado.
            if sio.connected:

                print(
                    "✅ SOCKET.IO CONFIRMADO COMO CONECTADO",
                    flush=True
                )

                inscrever_room()

                atualizar_status(
                    "COLETANDO",
                    "Coletor ativo"
                )

                # ------------------------------------------------
                # MANTER CONEXÃO
                # ------------------------------------------------
                while rodando and sio.connected:
                    time.sleep(1)

            else:
                print(
                    "⚠️ Socket.IO não ficou conectado",
                    flush=True
                )

        except Exception as e:

            print("=" * 72, flush=True)
            print("❌ ERRO NO COLETOR", flush=True)
            print(
                f"{type(e).__name__}: {e}",
                flush=True
            )
            traceback.print_exc()
            print("=" * 72, flush=True)

            try:
                atualizar_status(
                    "ERRO",
                    f"{type(e).__name__}: {e}"
                )
            except Exception:
                pass

        finally:

            try:
                if sio is not None and sio.connected:
                    sio.disconnect()
            except Exception:
                pass

        if rodando:
            print(
                "🔄 Reconectando em 5 segundos...",
                flush=True
            )

            time.sleep(5)


# ============================================================
# THREAD
# ============================================================

def iniciar_coletor_em_thread():
    thread = threading.Thread(
        target=executar_coletor,
        name="blaze-collector",
        daemon=True
    )

    thread.start()

    return thread


# ============================================================
# SNAPSHOT
# ============================================================

def snapshot_estado():
    return {
        "rodando": rodando,
        "socket_connected": bool(
            sio is not None and sio.connected
        ),
        "room": ROOM,
        "socket_path": SOCKET_PATH,
    }


# ============================================================
# EXECUÇÃO DIRETA
# ============================================================

if __name__ == "__main__":
    executar_coletor()
