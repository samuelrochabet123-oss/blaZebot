# ================================================================
# BLAZE DOUBLE — COLLECTOR V2.1 RENDER TEST
# ================================================================
# Objetivo:
#   Testar no Render o MESMO protocolo Socket.IO do Collector V2.1
#   que funcionava no Colab.
#
# IMPORTANTE:
#   - Não usa a API REST /roulette_games/recent/1
#   - Usa Socket.IO /replication/
#   - Mantém o subscribe original:
#
#       {
#           "id": "subscribe",
#           "payload": {
#               "room": "double_room_1"
#           }
#       }
#
#   - DATABASE_URL vem exclusivamente do ambiente do Render.
#   - Não colocar senha diretamente neste arquivo.
# ================================================================

import os
import sys
import time
import signal
import threading
from datetime import datetime

import socketio
import psycopg2


# ================================================================
# CONFIGURAÇÃO
# ================================================================

BLAZE_URL = "https://api-gaming.blaze.bet.br"
SOCKET_PATH = "/replication/"
ROOM = "double_room_1"
EVENT_NAME = "data"
TICK_NAME = "double.tick"

MOSTRAR_TICKS = True
INTERVALO_STATUS = 30
MAX_RECONEXOES = 999999


rodando = True
conectado = False
sio = None

ultima_rodada = None
total_ticks = 0
total_resultados = 0
total_duplicados = 0
total_erros_db = 0


CORES = {
    0: "BRANCO",
    1: "VERMELHO",
    2: "PRETO",
}


def nome_cor(cor):
    try:
        return CORES.get(int(cor), f"DESCONHECIDA({cor})")
    except Exception:
        return "DESCONHECIDA"


# ================================================================
# POSTGRESQL / NEON
# ================================================================

def get_db_connection():
    database_url = os.environ.get("DATABASE_URL")

    if not database_url:
        print("❌ DATABASE_URL não configurada no Render.")
        return None

    try:
        return psycopg2.connect(
            database_url,
            connect_timeout=15,
        )
    except Exception as e:
        print(f"❌ Erro PostgreSQL: {e}")
        return None


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
                    room_id INTEGER,
                    created_at TIMESTAMP,
                    updated_at TIMESTAMP,
                    coletado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # Compatibilidade com tabela já existente no Neon.
            cur.execute("""
                ALTER TABLE blaze_historico
                ADD COLUMN IF NOT EXISTS color INTEGER;
            """)
            cur.execute("""
                ALTER TABLE blaze_historico
                ADD COLUMN IF NOT EXISTS cor VARCHAR(20);
            """)
            cur.execute("""
                ALTER TABLE blaze_historico
                ADD COLUMN IF NOT EXISTS roll INTEGER;
            """)
            cur.execute("""
                ALTER TABLE blaze_historico
                ADD COLUMN IF NOT EXISTS status VARCHAR(30);
            """)
            cur.execute("""
                ALTER TABLE blaze_historico
                ADD COLUMN IF NOT EXISTS room_id INTEGER;
            """)
            cur.execute("""
                ALTER TABLE blaze_historico
                ADD COLUMN IF NOT EXISTS created_at TIMESTAMP;
            """)
            cur.execute("""
                ALTER TABLE blaze_historico
                ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP;
            """)
            cur.execute("""
                ALTER TABLE blaze_historico
                ADD COLUMN IF NOT EXISTS coletado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP;
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

        conn.commit()
        conn.close()

        print("✅ PostgreSQL/Neon OK")
        return True

    except Exception as e:
        print(f"❌ Erro inicializando banco: {e}")
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass
        return False


def salvar_resultado(payload):
    global total_resultados
    global total_duplicados
    global total_erros_db
    global ultima_rodada

    rodada_id = payload.get("id")
    color = payload.get("color")
    roll = payload.get("roll")
    status = payload.get("status")
    room_id = payload.get("room_id")

    created_at = payload.get("created_at")
    updated_at = payload.get("updated_at")

    if not rodada_id or color is None or roll is None:
        return False

    if status != "complete":
        return False

    try:
        color = int(color)
        roll = int(roll)
    except Exception:
        print(f"⚠️ Dados inválidos: id={rodada_id}")
        return False

    try:
        room_id = int(room_id) if room_id is not None else None
    except Exception:
        room_id = None

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
        total_erros_db += 1
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
                ON CONFLICT (rodada_id) DO NOTHING
                RETURNING id;
            """, (
                str(rodada_id),
                color,
                nome_cor(color),
                roll,
                status,
                room_id,
                created_datetime,
                updated_datetime,
            ))

            inserted = cur.fetchone()

        conn.commit()
        conn.close()

        if inserted:
            total_resultados += 1
            ultima_rodada = str(rodada_id)

            print("")
            print("=" * 70)
            print("✅ RESULTADO SALVO NO NEON")
            print(f"Rodada : {rodada_id}")
            print(f"Cor    : {nome_cor(color)}")
            print(f"Color  : {color}")
            print(f"Roll   : {roll}")
            print(f"Status : {status}")
            print(f"Room   : {room_id}")
            print("=" * 70)
            print("")

            return True

        total_duplicados += 1

        if MOSTRAR_TICKS:
            print(
                f"↩️ DUPLICADO | {rodada_id} | "
                f"{nome_cor(color)} | roll={roll}"
            )

        return False

    except Exception as e:
        total_erros_db += 1

        print(f"❌ Erro salvando no Neon: {e}")

        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass

        return False


# ================================================================
# SOCKET.IO — MESMO PROTOCOLO DO V2.1
# ================================================================

def on_connect():
    global conectado

    conectado = True

    print("")
    print("=" * 70)
    print("🟢 SOCKET.IO CONECTADO AO BLAZE")
    print("=" * 70)
    print(f"Servidor : {BLAZE_URL}")
    print(f"Path     : {SOCKET_PATH}")
    print(f"Namespace: /")
    print(f"Room     : {ROOM}")
    print("=" * 70)

    # IMPORTANTE:
    # Este é EXATAMENTE o subscribe do V2.1 que funcionava no Colab.
    try:
        payload = {
            "id": "subscribe",
            "payload": {
                "room": ROOM
            }
        }

        print("📡 ENVIANDO SUBSCRIBE ORIGINAL V2.1")
        print(f"   evento  = cmd")
        print(f"   payload = {payload}")

        sio.emit("cmd", payload)

        print("✅ SUBSCRIBE ENVIADO")

    except Exception as e:
        print(f"❌ Erro enviando subscribe: {e}")


def on_disconnect():
    global conectado

    conectado = False

    print("")
    print("=" * 70)
    print("🔴 SOCKET.IO DESCONECTADO")
    print("=" * 70)


def on_connect_error(data):
    global conectado

    conectado = False

    print("")
    print("=" * 70)
    print("❌ ERRO DE CONEXÃO SOCKET.IO")
    print("=" * 70)
    print(data)
    print("=" * 70)


def on_data(data):
    global total_ticks

    total_ticks += 1

    print("")
    print("📥 EVENTO DATA RECEBIDO")
    print(f"   tipo = {type(data).__name__}")

    if MOSTRAR_TICKS:
        print(f"   data = {data}")

    if not isinstance(data, dict):
        return

    event_id = data.get("id")
    payload = data.get("payload")

    if event_id != TICK_NAME:
        print(f"ℹ️ DATA recebido, mas id={event_id!r}")
        return

    if not isinstance(payload, dict):
        print("⚠️ double.tick sem payload dict")
        return

    rodada_id = payload.get("id")
    status = payload.get("status")
    color = payload.get("color")
    roll = payload.get("roll")

    print(
        f"🎲 TICK | ID={rodada_id} | "
        f"STATUS={status} | "
        f"COR={nome_cor(color) if color is not None else '-'} | "
        f"ROLL={roll}"
    )

    if status == "complete" and color is not None and roll is not None:
        salvar_resultado(payload)


# ================================================================
# MONITOR
# ================================================================

def monitor_status():
    while rodando:
        time.sleep(INTERVALO_STATUS)

        if not rodando:
            break

        print("")
        print(
            f"📊 STATUS | "
            f"Socket={'CONECTADO' if conectado else 'DESCONECTADO'} | "
            f"Ticks={total_ticks} | "
            f"Resultados={total_resultados} | "
            f"Duplicados={total_duplicados} | "
            f"ErrosDB={total_erros_db}"
        )


# ================================================================
# ENCERRAMENTO
# ================================================================

def encerrar(sig=None, frame=None):
    global rodando

    if not rodando:
        return

    print("")
    print("🛑 Encerrando collector...")

    rodando = False

    try:
        if sio:
            sio.disconnect()
    except Exception:
        pass


# ================================================================
# INICIAR SOCKET
# ================================================================

def iniciar_socket():
    global sio

    sio = socketio.Client(
        reconnection=True,
        reconnection_attempts=MAX_RECONEXOES,
        reconnection_delay=2,
        reconnection_delay_max=10,
        logger=False,
        engineio_logger=False,
    )

    sio.on("connect", on_connect)
    sio.on("disconnect", on_disconnect)
    sio.on("connect_error", on_connect_error)
    sio.on(EVENT_NAME, on_data)

    print("")
    print("=" * 70)
    print("CONECTANDO AO BLAZE DOUBLE")
    print("=" * 70)
    print(f"URL    : {BLAZE_URL}")
    print(f"SOCKET : {SOCKET_PATH}")
    print(f"ROOM   : {ROOM}")
    print("Transporte: websocket")
    print("=" * 70)

    try:
        sio.connect(
            BLAZE_URL,
            socketio_path=SOCKET_PATH,
            transports=["websocket"],
            wait_timeout=20,
        )

        return True

    except Exception as e:
        print("")
        print("=" * 70)
        print("❌ FALHA AO CONECTAR AO SOCKET.IO")
        print("=" * 70)
        print(e)
        print("=" * 70)
        return False


# ================================================================
# MAIN
# ================================================================

def main():
    global rodando

    print("")
    print("=" * 70)
    print("BLAZE DOUBLE — RENDER TEST V2.1")
    print("=" * 70)
    print("Teste do protocolo Socket.IO original do Colab")
    print("=" * 70)

    if not os.environ.get("DATABASE_URL"):
        print("❌ DATABASE_URL não encontrada.")
        sys.exit(1)

    print("✅ DATABASE_URL encontrada no ambiente.")

    if not init_db():
        print("❌ Banco não inicializado.")
        sys.exit(1)

    signal.signal(signal.SIGTERM, encerrar)
    signal.signal(signal.SIGINT, encerrar)

    thread = threading.Thread(
        target=monitor_status,
        daemon=True,
    )
    thread.start()

    while rodando:
        print("")
        print("🔄 Iniciando conexão...")

        sucesso = iniciar_socket()

        if not sucesso:
            print("🔄 Nova tentativa em 5 segundos...")
            time.sleep(5)
            continue

        print("")
        print("🟢 COLLECTOR TESTE OPERANDO")
        print("Aguardando eventos data / double.tick...")
        print("")

        # Mantém a conexão viva.
        while rodando and sio and sio.connected:
            time.sleep(1)

        if rodando:
            print("⚠️ Socket desconectado.")
            print("🔄 Reconectando em 5 segundos...")
            time.sleep(5)

    print("Collector encerrado.")


if __name__ == "__main__":
    main()
