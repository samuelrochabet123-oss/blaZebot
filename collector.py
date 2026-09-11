# =============================================================================
# BLAZE COLLECTOR V2.2 — DIAGNÓSTICO SOCKET.IO
# Render + Neon PostgreSQL
#
# OBJETIVO DESTA VERSÃO:
# - NÃO altera a lógica do banco além da compatibilidade necessária.
# - NÃO faz apostas nem possui estratégia.
# - NÃO usa REST /roulette_games/recent/1.
# - Conecta ao Socket.IO da Blaze.
# - Envia a inscrição da room.
# - MOSTRA EVENTOS RECEBIDOS para descobrir por que nenhum double.tick
#   está chegando ao Render.
# =============================================================================

import os
import time
import json
import threading
from datetime import datetime, timezone

import psycopg2
import socketio


# =============================================================================
# CONFIGURAÇÃO
# =============================================================================

BLAZE_URL = "https://api-gaming.blaze.bet.br"
SOCKET_PATH = "/replication/"
ROOM = "double_room_1"
TICK_NAME = "double.tick"

DATABASE_URL = os.getenv("DATABASE_URL")

CORES = {
    0: "BRANCO",
    1: "VERMELHO",
    2: "PRETO",
}

rodando = True
ultimo_evento = None
contador_eventos = 0
ultimo_tick = None


# =============================================================================
# BANCO
# =============================================================================

def get_db_connection():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL não configurada no Render.")

    url = DATABASE_URL

    if "sslmode=" not in url:
        url += "&sslmode=require" if "?" in url else "?sslmode=require"

    return psycopg2.connect(url)


def init_db():
    conn = None
    cur = None

    try:
        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS blaze_historico (
                id BIGSERIAL PRIMARY KEY,
                rodada_id TEXT UNIQUE,
                color INTEGER,
                cor TEXT,
                roll INTEGER,
                status TEXT,
                room_id INTEGER,
                created_at TIMESTAMP,
                updated_at TIMESTAMP,
                coletado_em TIMESTAMP
            )
        """)

        # Compatibilidade com tabelas antigas já existentes no Neon.
        colunas = {
            "rodada_id": "TEXT",
            "color": "INTEGER",
            "cor": "TEXT",
            "roll": "INTEGER",
            "status": "TEXT",
            "room_id": "INTEGER",
            "created_at": "TIMESTAMP",
            "updated_at": "TIMESTAMP",
            "coletado_em": "TIMESTAMP",
        }

        for coluna, tipo in colunas.items():
            cur.execute(
                f"ALTER TABLE blaze_historico "
                f"ADD COLUMN IF NOT EXISTS {coluna} {tipo}"
            )

        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_blaze_historico_rodada
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
                mensagem TEXT,
                ultimo_tick TIMESTAMP,
                atualizado_em TIMESTAMP
            )
        """)

        status_cols = {
            "status": "TEXT",
            "mensagem": "TEXT",
            "ultimo_tick": "TIMESTAMP",
            "atualizado_em": "TIMESTAMP",
        }

        for coluna, tipo in status_cols.items():
            cur.execute(
                f"ALTER TABLE collector_status "
                f"ADD COLUMN IF NOT EXISTS {coluna} {tipo}"
            )

        conn.commit()

        print("✅ Banco PostgreSQL verificado/migrado", flush=True)
        print("✅ PostgreSQL/Neon OK", flush=True)

    except Exception as e:
        print(f"❌ Erro ao inicializar banco: {e}", flush=True)
        raise

    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


def atualizar_status(status, mensagem="", ultimo_tick=None):
    conn = None
    cur = None

    try:
        conn = get_db_connection()
        cur = conn.cursor()

        agora = datetime.now(timezone.utc).replace(tzinfo=None)

        cur.execute("""
            UPDATE collector_status
               SET status = %s,
                   mensagem = %s,
                   ultimo_tick = %s,
                   atualizado_em = %s
             WHERE id = 1
        """, (
            status,
            mensagem,
            ultimo_tick,
            agora,
        ))

        if cur.rowcount == 0:
            cur.execute("""
                INSERT INTO collector_status
                    (id, status, mensagem, ultimo_tick, atualizado_em)
                VALUES
                    (1, %s, %s, %s, %s)
            """, (
                status,
                mensagem,
                ultimo_tick,
                agora,
            ))

        conn.commit()

    except Exception as e:
        print(f"⚠️ Erro ao atualizar status do coletor: {e}", flush=True)

    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


# =============================================================================
# SALVAMENTO
# =============================================================================

def converter_datetime(valor):
    if not valor:
        return None

    if isinstance(valor, datetime):
        if valor.tzinfo:
            return valor.astimezone(timezone.utc).replace(tzinfo=None)
        return valor

    try:
        texto = str(valor).replace("Z", "+00:00")
        dt = datetime.fromisoformat(texto)

        if dt.tzinfo:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)

        return dt

    except Exception:
        return None


def salvar_resultado(payload):
    if not isinstance(payload, dict):
        return False

    status = str(payload.get("status", "")).lower()

    if status != "complete":
        return False

    rodada_id = (
        payload.get("id")
        or payload.get("round_id")
        or payload.get("rodada_id")
    )

    color = payload.get("color")
    roll = payload.get("roll")

    if rodada_id is None or color is None or roll is None:
        return False

    try:
        color = int(color)
        roll = int(roll)
    except Exception:
        return False

    cor = CORES.get(color, f"DESCONHECIDA({color})")

    room_id = payload.get("room_id")

    try:
        room_id = int(room_id) if room_id is not None else None
    except Exception:
        room_id = None

    created_at = converter_datetime(
        payload.get("created_at")
        or payload.get("createdAt")
    )

    updated_at = converter_datetime(
        payload.get("updated_at")
        or payload.get("updatedAt")
    )

    agora = datetime.now(timezone.utc).replace(tzinfo=None)

    if created_at is None:
        created_at = agora

    if updated_at is None:
        updated_at = agora

    conn = None
    cur = None

    try:
        conn = get_db_connection()
        cur = conn.cursor()

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
                (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (rodada_id) DO NOTHING
        """, (
            str(rodada_id),
            color,
            cor,
            roll,
            status,
            room_id,
            created_at,
            updated_at,
            agora,
        ))

        inserido = cur.rowcount > 0
        conn.commit()

        if inserido:
            print(
                f"✅ RESULTADO SALVO | rodada={rodada_id} "
                f"| roll={roll} | cor={cor}",
                flush=True
            )
        else:
            print(
                f"ℹ️ RESULTADO JÁ EXISTIA | rodada={rodada_id}",
                flush=True
            )

        atualizar_status(
            "ONLINE",
            f"Último resultado: {rodada_id} | {cor} | roll={roll}",
            agora,
        )

        return inserido

    except Exception as e:
        print(f"❌ Erro ao salvar resultado: {e}", flush=True)
        return False

    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


# =============================================================================
# SOCKET.IO
# =============================================================================

sio = socketio.Client(
    reconnection=False,
    logger=False,
    engineio_logger=False,
)


def resumo_evento(data, limite=2000):
    try:
        texto = json.dumps(
            data,
            ensure_ascii=False,
            default=str,
        )
    except Exception:
        texto = repr(data)

    if len(texto) > limite:
        texto = texto[:limite] + "... [TRUNCADO]"

    return texto


def registrar_evento(nome_evento, data):
    global ultimo_evento, contador_eventos

    contador_eventos += 1
    ultimo_evento = nome_evento

    print("", flush=True)
    print("=" * 78, flush=True)
    print(f"📥 EVENTO RECEBIDO #{contador_eventos}", flush=True)
    print(f"   Evento : {nome_evento}", flush=True)
    print(f"   Tipo   : {type(data).__name__}", flush=True)
    print(f"   Dados  : {resumo_evento(data)}", flush=True)
    print("=" * 78, flush=True)

    atualizar_status(
        "ONLINE",
        f"Evento recebido: {nome_evento} | #{contador_eventos}",
        None,
    )


@sio.event
def connect():
    print("🟢 SOCKET.IO CONECTADO AO BLAZE", flush=True)
    print("🟢 Namespace '/' conectado", flush=True)

    atualizar_status(
        "CONECTADO",
        f"Socket conectado. Room alvo: {ROOM}",
        None,
    )


@sio.event
def disconnect():
    print("🔴 SOCKET.IO DESCONECTADO", flush=True)

    atualizar_status(
        "DESCONECTADO",
        "Socket.IO desconectado",
        None,
    )


@sio.event
def connect_error(data):
    print(f"❌ SOCKET.IO CONNECT_ERROR: {resumo_evento(data)}", flush=True)

    atualizar_status(
        "ERRO",
        f"connect_error: {resumo_evento(data, 500)}",
        None,
    )


# -----------------------------------------------------------------------------
# EVENTO "data" — captura tudo, depois tenta reconhecer double.tick
# -----------------------------------------------------------------------------

@sio.on("data")
def on_data(data):
    global ultimo_tick

    registrar_evento("data", data)

    if not isinstance(data, dict):
        return

    event_id = data.get("id")

    if event_id == TICK_NAME:
        ultimo_tick = datetime.now(timezone.utc).replace(tzinfo=None)

        print("🎯 DOUBLE.TICK ENCONTRADO!", flush=True)

        payload = (
            data.get("data")
            or data.get("payload")
            or data.get("result")
        )

        print(
            f"   Payload: {resumo_evento(payload)}",
            flush=True
        )

        if isinstance(payload, dict):
            salvar_resultado(payload)


# -----------------------------------------------------------------------------
# CATCH-ALL
#
# python-socketio permite registrar '*' para observar eventos recebidos.
# Isso é importante para descobrir se a Blaze está enviando outro nome de
# evento em vez de "data".
# -----------------------------------------------------------------------------

@sio.on("*")
def catch_all(event, data):
    if event == "data":
        return

    registrar_evento(
        f"CATCH-ALL: {event}",
        data,
    )


# =============================================================================
# INSCRIÇÃO
# =============================================================================

def inscrever_room():
    if not sio.connected:
        print("⚠️ Não é possível inscrever: Socket.IO não conectado", flush=True)
        return False

    try:
        print("", flush=True)
        print("📡 ENVIANDO INSCRIÇÃO", flush=True)
        print(f"   room = {ROOM}", flush=True)
        print(
            '   payload = {"room": "double_room_1"}',
            flush=True
        )

        sio.emit("cmd", {"room": ROOM})

        print("📡 INSCRIÇÃO ENVIADA", flush=True)

        atualizar_status(
            "CONECTADO",
            f"Inscrição enviada para {ROOM}; aguardando eventos...",
            None,
        )

        return True

    except Exception as e:
        print(f"❌ Erro ao enviar inscrição: {e}", flush=True)

        atualizar_status(
            "ERRO",
            f"Erro na inscrição: {e}",
            None,
        )

        return False


# =============================================================================
# CONEXÃO
# =============================================================================

def conectar():
    try:
        print("🔌 Conectando ao Socket.IO da Blaze...", flush=True)

        sio.connect(
            BLAZE_URL,
            socketio_path=SOCKET_PATH,
            transports=["websocket"],
            wait_timeout=20,
        )

        if sio.connected:
            print("✅ SOCKET.IO CONFIRMADO COMO CONECTADO", flush=True)

            # IMPORTANTE:
            # A inscrição acontece somente depois que sio.connect() retorna.
            inscrever_room()

            return True

        print("❌ sio.connected=False após connect()", flush=True)
        return False

    except Exception as e:
        print(f"❌ Falha ao conectar Socket.IO: {e}", flush=True)

        atualizar_status(
            "ERRO",
            f"Falha Socket.IO: {e}",
            None,
        )

        return False


# =============================================================================
# LOOP PRINCIPAL
# =============================================================================

def executar():
    global rodando

    print("", flush=True)
    print("=" * 78, flush=True)
    print("🎰 BLAZE COLLECTOR — DIAGNÓSTICO SOCKET.IO", flush=True)
    print("=" * 78, flush=True)
    print(f"🌐 Blaze       : {BLAZE_URL}", flush=True)
    print(f"🔌 Socket Path : {SOCKET_PATH}", flush=True)
    print(f"🏠 Room        : {ROOM}", flush=True)
    print(f"🎯 Tick alvo   : {TICK_NAME}", flush=True)
    print("=" * 78, flush=True)

    print("🔌 Verificando PostgreSQL/Neon...", flush=True)
    init_db()

    while rodando:
        try:
            if not sio.connected:
                print("🔄 Iniciando nova conexão...", flush=True)

                conectado = conectar()

                if not conectado:
                    print(
                        "🔄 Reconectando em 5 segundos...",
                        flush=True
                    )
                    time.sleep(5)
                    continue

            # Mantém o cliente vivo e processando eventos.
            while rodando and sio.connected:
                time.sleep(1)

            if rodando:
                print(
                    "🔴 Conexão encerrada pelo servidor.",
                    flush=True
                )
                print(
                    "🔄 Reconectando em 5 segundos...",
                    flush=True
                )
                time.sleep(5)

        except Exception as e:
            print(f"❌ Erro no loop principal: {e}", flush=True)

            try:
                if sio.connected:
                    sio.disconnect()
            except Exception:
                pass

            time.sleep(5)


# =============================================================================
# START
# =============================================================================

def iniciar_coletor_em_thread():
    thread = threading.Thread(
        target=executar,
        daemon=True,
        name="blaze-collector",
    )

    thread.start()

    return thread


if __name__ == "__main__":
    executar()
