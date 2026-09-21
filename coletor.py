# ================================================================
# BLAZE DOUBLE — COLLECTOR V2.1 — GOOGLE PLANILHAS EDITION
# ================================================================
# Coleta resultados do Double da Blaze via Socket.IO e
# persiste em Google Planilhas (sheets_db.py).
#
# GOOGLE_SHEETS_ID e credenciais vêm do ambiente (Render).
# NÃO colocar credenciais diretamente neste arquivo.
# ================================================================

import os
import sys
import time
import signal
import threading
from datetime import datetime

import socketio

import sheets_db as db
from strategy_engine import processar_novo_resultado


# ================================================================
# CONFIGURAÇÃO
# ================================================================

BLAZE_URL = "https://api-gaming.blaze.bet.br"
SOCKET_PATH = "/replication/"
ROOM = "double_room_1"
EVENT_NAME = "data"
TICK_NAME = "double.tick"

MOSTRAR_TICKS = False
INTERVALO_STATUS = 30
MAX_RECONEXOES = 999999


rodando = True
conectado = False
sio = None

ultima_rodada = None
ultimo_resultado_em = None
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
# GOOGLE PLANILHAS
# ================================================================

def init_db():
    return db.init_db()


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

    def fmt_dt(valor):
        try:
            return datetime.fromisoformat(
                str(valor).replace("Z", "+00:00")
            ).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return ""

    try:
        novo_id = db.inserir_rodada(
            rodada_id=str(rodada_id),
            color=color,
            cor_nome=nome_cor(color),
            roll=roll,
            status=status,
            room_id=room_id,
            created_at=fmt_dt(created_at),
            updated_at=fmt_dt(updated_at),
        )

        if novo_id:
            total_resultados += 1
            ultima_rodada = str(rodada_id)
            ultimo_resultado_em = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

            print("")
            print("=" * 70)
            print("✅ RESULTADO SALVO NO GOOGLE PLANILHAS")
            print(f"Rodada : {rodada_id}")
            print(f"Cor    : {nome_cor(color)}")
            print(f"Color  : {color}")
            print(f"Roll   : {roll}")
            print(f"Status : {status}")
            print(f"Room   : {room_id}")
            print("=" * 70)
            print("")

            # ============================================================
            # MOTOR DAS ESTRATÉGIAS
            # ============================================================
            try:
                processar_novo_resultado(
                    rodada_id=str(rodada_id),
                    color=color,
                    roll=roll,
                )
            except Exception as e:
                print(f"⚠️ Erro no motor das estratégias: {e}")

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

        print(f"❌ Erro salvando no Google Planilhas: {e}")

        return False


def persistir_status():
    """Grava o status do collector na planilha (aba collector_status)."""
    try:
        db.atualizar_collector({
            "conectado": conectado,
            "ultima_rodada": ultima_rodada or "",
            "ultimo_resultado_em": ultimo_resultado_em or "",
            "total_ticks": total_ticks,
            "total_resultados": total_resultados,
            "total_duplicados": total_duplicados,
            "total_erros_db": total_erros_db,
        })
    except Exception as e:
        print(f"⚠️ Erro persistindo status do collector: {e}")


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

        persistir_status()

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
        persistir_status()
    except Exception:
        pass

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


def iniciar_coletor_em_thread():
    """
    Compatibilidade com o app.py do Render.

    Inicia o collector em uma thread daemon para que o Gunicorn
    consiga subir o Flask normalmente enquanto o Socket.IO roda
    em segundo plano.
    """
    thread = threading.Thread(
        target=main,
        name="blaze-collector",
        daemon=True,
    )
    thread.start()
    print("🚀 Collector V2.1 iniciado em background", flush=True)
    return thread


def main():
    global rodando

    print("")
    print("=" * 70)
    print("BLAZE DOUBLE — RENDER TEST V2.1 — GOOGLE PLANILHAS")
    print("=" * 70)
    print("Teste do protocolo Socket.IO original do Colab")
    print("=" * 70)

    if not os.environ.get("GOOGLE_SHEETS_ID"):
        print("❌ GOOGLE_SHEETS_ID não encontrada.")
        sys.exit(1)

    if not db.credenciais_disponiveis():
        print("❌ Credenciais Google não encontradas "
              "(GOOGLE_CREDENTIALS_JSON ou GOOGLE_CREDENTIALS).")
        sys.exit(1)

    print("✅ GOOGLE_SHEETS_ID encontrada no ambiente.")

    if not init_db():
        print("❌ Planilha não inicializada.")
        sys.exit(1)

    # Signals só podem ser registrados na thread principal.
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, encerrar)
        signal.signal(signal.SIGINT, encerrar)
    else:
        print("ℹ️ Collector em background: registro de signals ignorado.", flush=True)

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
