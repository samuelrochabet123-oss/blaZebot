import os
import time
import threading
import traceback
from datetime import datetime, timezone

import psycopg2
import socketio


# ============================================================
# CONFIGURAÇÃO
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
# BANCO
# ============================================================

def get_db_connection():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL não configurada")

    url = DATABASE_URL

    if "sslmode=" not in url:
        separator = "&" if "?" in url else "?"
        url += f"{separator}sslmode=require"

    return psycopg2.connect(url)


def init_db():
    conn = get_db_connection()

    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS blaze_historico (
                    id BIGSERIAL PRIMARY KEY,
                    rodada_id TEXT UNIQUE NOT NULL,
                    color TEXT,
                    cor TEXT,
                    roll INTEGER,
                    status TEXT,
                    room_id TEXT,
                    created_at TIMESTAMPTZ,
                    updated_at TIMESTAMPTZ,
                    coletado_em TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_blaze_historico_rodada
                ON blaze_historico (rodada_id)
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_blaze_historico_coletado
                ON blaze_historico (coletado_em DESC)
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS collector_status (
                    id INTEGER PRIMARY KEY,
                    status TEXT,
                    ultima_atualizacao TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    ultima_rodada TEXT,
                    ultima_cor TEXT,
                    ultimo_roll INTEGER,
                    mensagem TEXT
                )
            """)

        conn.commit()

    finally:
        conn.close()


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
                cur.execute("""
                    INSERT INTO collector_status
                        (
                            id,
                            status,
                            ultima_atualizacao,
                            ultima_rodada,
                            ultima_cor,
                            ultimo_roll,
                            mensagem
                        )
                    VALUES
                        (1, %s, NOW(), %s, %s, %s, %s)
                    ON CONFLICT (id)
                    DO UPDATE SET
                        status = EXCLUDED.status,
                        ultima_atualizacao = NOW(),
                        ultima_rodada = COALESCE(
                            EXCLUDED.ultima_rodada,
                            collector_status.ultima_rodada
                        ),
                        ultima_cor = COALESCE(
                            EXCLUDED.ultima_cor,
                            collector_status.ultima_cor
                        ),
                        ultimo_roll = COALESCE(
                            EXCLUDED.ultimo_roll,
                            collector_status.ultimo_roll
                        ),
                        mensagem = EXCLUDED.mensagem
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
        print(f"⚠️ Erro ao atualizar status do coletor: {e}", flush=True)


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
        or payload.get("colour")
    )

    roll = (
        payload.get("roll")
        if payload.get("roll") is not None
        else payload.get("number")
    )

    status = payload.get("status")

    room_id = (
        payload.get("room_id")
        or payload.get("roomId")
        or ROOM
    )

    if rodada_id is None:
        return False

    if color is None or roll is None:
        return False

    try:
        roll = int(roll)
    except (TypeError, ValueError):
        return False

    rodada_id = str(rodada_id)
    color = str(color).lower()

    created_at = payload.get("created_at") or payload.get("createdAt")
    updated_at = payload.get("updated_at") or payload.get("updatedAt")

    conn = get_db_connection()

    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO blaze_historico
                    (
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
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (rodada_id) DO NOTHING
                RETURNING id
            """, (
                rodada_id,
                color,
                color,
                roll,
                status,
                room_id,
                created_at,
                updated_at
            ))

            inserted = cur.fetchone() is not None

        conn.commit()

    finally:
        conn.close()

    if inserted:
        print(
            f"💾 NOVO JOGO | rodada={rodada_id} "
            f"| cor={color} | roll={roll}",
            flush=True
        )

        atualizar_status(
            "COLETANDO",
            "Novo resultado recebido",
            rodada_id,
            color,
            roll
        )

    return inserted


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
        print("🟢 SOCKET.IO CONECTADO AO BLAZE", flush=True)

        atualizar_status(
            "CONECTADO",
            "Socket.IO conectado; aguardando inscrição"
        )

        # IMPORTANTE:
        # Não fazemos sio.emit() aqui.
        #
        # O erro anterior:
        # BadNamespaceError: '/' is not a connected namespace
        #
        # acontecia porque o callback connect() era disparado
        # durante o processo interno de conexão do namespace.
        #
        # A inscrição será feita pela thread principal depois
        # que connect() retornar e o cliente estiver efetivamente
        # conectado.

    @sio.event
    def disconnect():
        print("🔴 SOCKET.IO DESCONECTADO", flush=True)

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


def inscrever_room():
    """
    Envia a inscrição somente depois de sio.connect()
    ter retornado com o namespace conectado.
    """

    if sio is None:
        return False

    if not sio.connected:
        print(
            "⚠️ Tentativa de inscrição sem Socket.IO conectado",
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


def processar_tick(data):
    if not isinstance(data, dict):
        return

    if data.get("id") != TICK_NAME:
        return

    payload = data.get("payload")

    if not isinstance(payload, dict):
        return

    status = str(payload.get("status", "")).lower()

    if status != "complete":
        return

    salvar_resultado(payload)


# ============================================================
# EXECUÇÃO
# ============================================================

def executar_coletor():
    print("=" * 72, flush=True)
    print("🎰 BLAZE COLLECTOR - RENDER + NEON", flush=True)
    print("=" * 72, flush=True)
    print(f"🌐 Blaze: {BLAZE_URL}", flush=True)
    print(f"🔌 Socket path: {SOCKET_PATH}", flush=True)
    print(f"🏠 Room: {ROOM}", flush=True)
    print("=" * 72, flush=True)

    while rodando:

        try:
            print("🔌 Testando conexão com PostgreSQL...", flush=True)

            init_db()

            atualizar_status(
                "INICIANDO",
                "Inicializando coletor"
            )

            print("✅ PostgreSQL conectado", flush=True)

            criar_socket()

            print("🔌 Conectando ao Socket.IO...", flush=True)

            atualizar_status(
                "CONECTANDO",
                "Conectando ao Socket.IO da Blaze"
            )

            sio.connect(
                BLAZE_URL,
                socketio_path=SOCKET_PATH,
                transports=["websocket"],
                wait_timeout=20
            )

            # Neste ponto sio.connect() já retornou.
            # Agora verificamos explicitamente o estado antes
            # de enviar o comando "cmd".
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

                # Mantém a conexão viva.
                while rodando and sio.connected:
                    time.sleep(1)

            else:
                print(
                    "⚠️ sio.connect() retornou, mas sio.connected=False",
                    flush=True
                )

        except Exception as e:
            print("=" * 72, flush=True)
            print("❌ ERRO NO COLETOR", flush=True)
            print(f"{type(e).__name__}: {e}", flush=True)
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
# THREAD DO COLETOR
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


if __name__ == "__main__":
    executar_coletor()
