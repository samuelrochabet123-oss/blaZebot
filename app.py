import os
import time
import signal
import threading
from collections import deque
from datetime import datetime

import socketio
import psycopg2
from flask import Flask, redirect, render_template_string, request

# ================================================================
# BLAZE DOUBLE — BOT V3
# ================================================================
# Coleta contínua + PostgreSQL + motor V3 + painel web
#
# COLETA:
#   Blaze Socket.IO
#       -> double.tick
#       -> status=complete
#       -> blaze_historico
#
# OPERAÇÃO:
#   RED / BLACK são os únicos resultados avaliados.
#   WHITE permanece no histórico, mas NÃO é WIN nem LOSS.
#
# V3:
#   10 regras
#   mínimo operacional padrão = 2 votos
#   empate = sem sinal
#
# IMPORTANTE:
#   O bot NÃO executa apostas na Blaze. Ele apenas gera sinais
#   e contabiliza uma simulação de aposta fixa no painel.
# ================================================================


# ================================================================
# CONFIGURAÇÃO
# ================================================================

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

# Operação
MIN_CONFLUENCIA = int(os.getenv("MIN_CONFLUENCIA", "2"))
APOSTA_BASE = float(os.getenv("APOSTA_BASE", "1.00"))

# Memória utilizada pelas regras
HISTORICO_MEMORIA = int(os.getenv("HISTORICO_MEMORIA", "50"))

# Quantidade de linhas do console no painel
MAX_LOGS = 80

# Monitor
INTERVALO_STATUS = 30

# Reconexão praticamente contínua
MAX_RECONEXOES = 999999

# Se False, não mostra cada tick intermediário.
MOSTRAR_TICKS = os.getenv(
    "MOSTRAR_TICKS",
    "false"
).lower() == "true"


# ================================================================
# CORES
# ================================================================

CORES = {
    0: "BRANCO",
    1: "VERMELHO",
    2: "PRETO"
}

COR_SIGLA = {
    "BRANCO": "W",
    "VERMELHO": "R",
    "PRETO": "B"
}


def nome_cor(cor):
    try:
        return CORES.get(int(cor), f"DESCONHECIDA({cor})")
    except Exception:
        return "DESCONHECIDA"


def sigla_cor(cor):
    nome = nome_cor(cor)
    return COR_SIGLA.get(nome)


# ================================================================
# ESTADO GLOBAL
# ================================================================

sio = None

rodando = True
conectado = False
ultima_rodada = None
ultimo_resultado_em = None

total_ticks = 0
total_resultados = 0
total_duplicados = 0
total_erros_db = 0

# Histórico usado pelo motor V3.
history_numbers = deque(maxlen=HISTORICO_MEMORIA)
history_colors = deque(maxlen=HISTORICO_MEMORIA)

# IDs já processados nesta execução.
processed_issues = set()

# ================================================================
# ESTADO DO BOT
# ================================================================

bot_running = False

# CAÇANDO = aguardando sinal
# ACOMPANHANDO = existe um sinal aberto
bot_state = "PARADO"

signal_color = None
signal_issue = None
signal_votes = 0
signal_rules = []

# ================================================================
# PLACAR DA SESSÃO
# ================================================================

wins = 0
losses = 0
white_ignored = 0
current_profit = 0.0

history_results = deque(maxlen=30)

# Lock único para estado compartilhado entre Flask e Socket.IO.
state_lock = threading.RLock()

# Logs
log_lines = deque(maxlen=MAX_LOGS)


# ================================================================
# FLASK
# ================================================================

app = Flask(__name__)


# ================================================================
# LOG
# ================================================================

def add_log(msg):
    timestamp = datetime.now().strftime("%H:%M:%S")

    with state_lock:
        log_lines.append(
            f"[{timestamp}] {msg}"
        )


# ================================================================
# POSTGRESQL
# ================================================================

def get_db_connection():
    database_url = os.environ.get("DATABASE_URL")

    if not database_url:
        add_log("⚠️ DATABASE_URL não configurada.")
        return None

    try:
        return psycopg2.connect(
            database_url,
            connect_timeout=15
        )

    except Exception as e:
        add_log(
            f"⚠️ Erro PostgreSQL: {str(e)[:180]}"
        )
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
                CREATE TABLE IF NOT EXISTS blaze_operacoes (
                    id SERIAL PRIMARY KEY,
                    rodada_sinal VARCHAR(100),
                    rodada_resultado VARCHAR(100),
                    sinal VARCHAR(10),
                    resultado VARCHAR(20),
                    roll INTEGER,
                    votos INTEGER,
                    regras TEXT,
                    valor NUMERIC(12,2),
                    lucro NUMERIC(12,2),
                    criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            conn.commit()

        conn.close()

        add_log("🗄️ PostgreSQL pronto.")
        return True

    except Exception as e:
        add_log(
            f"❌ Erro inicializando banco: {e}"
        )

        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass

        return False


def salvar_resultado_db(payload):
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

    created_datetime = parse_timestamp(created_at)
    updated_datetime = parse_timestamp(updated_at)

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
                ON CONFLICT (rodada_id)
                DO NOTHING
                RETURNING id;
            """, (
                str(rodada_id),
                int(color),
                cor_nome,
                int(roll),
                status,
                room_id,
                created_datetime,
                updated_datetime
            ))

            inserted = cur.fetchone()

        conn.commit()
        conn.close()

        if inserted:
            total_resultados += 1
            ultima_rodada = str(rodada_id)
            ultimo_resultado_em = datetime.now()

            add_log(
                f"💾 SALVO | {str(rodada_id)} | "
                f"{cor_nome} | roll={roll}"
            )

            return True

        total_duplicados += 1
        return False

    except Exception as e:
        total_erros_db += 1

        add_log(
            f"❌ Erro salvando resultado: {str(e)[:180]}"
        )

        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass

        return False


def parse_timestamp(value):
    if not value:
        return None

    try:
        return datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        ).replace(tzinfo=None)

    except Exception:
        return None


def carregar_historico():
    """
    Carrega os últimos resultados do PostgreSQL para que o V3
    não precise esperar 3, 5 ou 10 novas rodadas após reiniciar.
    """

    conn = get_db_connection()

    if not conn:
        return False

    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    rodada_id,
                    roll,
                    cor
                FROM blaze_historico
                WHERE status = 'complete'
                ORDER BY
                    COALESCE(created_at, coletado_em) ASC,
                    id ASC;
            """)

            rows = cur.fetchall()

        conn.close()

        with state_lock:
            history_numbers.clear()
            history_colors.clear()

            processed_issues.clear()

            for rodada_id, roll, cor in rows:
                if roll is None:
                    continue

                sigla = {
                    "BRANCO": "W",
                    "VERMELHO": "R",
                    "PRETO": "B"
                }.get(str(cor).upper())

                if sigla is None:
                    continue

                history_numbers.append(int(roll))
                history_colors.append(sigla)

                processed_issues.add(str(rodada_id))

        add_log(
            f"📚 Histórico carregado: "
            f"{len(history_numbers)} resultados em memória."
        )

        return True

    except Exception as e:
        add_log(
            f"⚠️ Erro carregando histórico: {e}"
        )

        try:
            conn.close()
        except Exception:
            pass

        return False


def salvar_operacao_db(
    rodada_sinal,
    rodada_resultado,
    sinal,
    resultado,
    roll,
    votos,
    regras,
    valor,
    lucro
):
    conn = get_db_connection()

    if not conn:
        return

    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO blaze_operacoes (
                    rodada_sinal,
                    rodada_resultado,
                    sinal,
                    resultado,
                    roll,
                    votos,
                    regras,
                    valor,
                    lucro
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s);
            """, (
                rodada_sinal,
                rodada_resultado,
                sinal,
                resultado,
                roll,
                votos,
                regras,
                valor,
                lucro
            ))

        conn.commit()
        conn.close()

    except Exception as e:
        add_log(
            f"⚠️ Erro salvando operação: {str(e)[:160]}"
        )

        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass


# ================================================================
# REGRAS V3
# ================================================================

def calcular_regras():
    """
    Retorna um dicionário:
        {
            "R1": "B",
            "R2": "R",
            ...
        }

    None significa que a regra não gerou sinal.

    IMPORTANTE:
    O resultado atual NÃO está no histórico neste momento.
    Portanto as regras usam apenas resultados anteriores.
    """

    n = list(history_numbers)
    c = list(history_colors)

    sinais = {}

    # Precisamos de pelo menos 2 resultados para algumas regras,
    # mas mantemos todas as regras disponíveis desde o início.

    # ------------------------------------------------------------
    # R1 — SOMA2 <= 8 -> BLACK
    # ------------------------------------------------------------
    if len(n) >= 2:
        soma2 = n[-1] + n[-2]

        if soma2 <= 8:
            sinais["R1"] = "B"

    # ------------------------------------------------------------
    # R2 — SOMA3 <= 14 -> RED
    # ------------------------------------------------------------
    if len(n) >= 3:
        soma3 = sum(n[-3:])

        if soma3 <= 14:
            sinais["R2"] = "R"

    # ------------------------------------------------------------
    # R3 — SOMA3 >= 30 -> BLACK
    # ------------------------------------------------------------
    if len(n) >= 3:
        soma3 = sum(n[-3:])

        if soma3 >= 30:
            sinais["R3"] = "B"

    # ------------------------------------------------------------
    # R4 — DISTÂNCIA >= 9 -> RED
    # ------------------------------------------------------------
    if len(n) >= 2:
        distancia = abs(n[-1] - n[-2])

        if distancia >= 9:
            sinais["R4"] = "R"

    # ------------------------------------------------------------
    # R5 — DISTÂNCIA >= 11 -> RED
    # ------------------------------------------------------------
    if len(n) >= 2:
        distancia = abs(n[-1] - n[-2])

        if distancia >= 11:
            sinais["R5"] = "R"

    # ------------------------------------------------------------
    # R6 — NÚMERO 3 -> RED
    # ------------------------------------------------------------
    if len(n) >= 1:
        if n[-1] == 3:
            sinais["R6"] = "R"

    # ------------------------------------------------------------
    # R7 — NÚMERO 4 -> BLACK
    # ------------------------------------------------------------
    if len(n) >= 1:
        if n[-1] == 4:
            sinais["R7"] = "B"

    # ------------------------------------------------------------
    # R8 — NÚMERO 6 -> BLACK
    # ------------------------------------------------------------
    if len(n) >= 1:
        if n[-1] == 6:
            sinais["R8"] = "B"

    # ------------------------------------------------------------
    # R9 — H10-H10-L7 -> BLACK
    # n3 >= 8, n2 >= 8, n1 == 7
    # ------------------------------------------------------------
    if len(n) >= 3:
        if (
            n[-3] >= 8
            and n[-2] >= 8
            and n[-1] == 7
        ):
            sinais["R9"] = "B"

    # ------------------------------------------------------------
    # R10 — H7-H7-L1 -> BLACK
    # n3 >= 7, n2 >= 7, n1 <= 1
    # ------------------------------------------------------------
    if len(n) >= 3:
        if (
            n[-3] >= 7
            and n[-2] >= 7
            and n[-1] <= 1
        ):
            sinais["R10"] = "B"

    return sinais


def calcular_confluencia():
    sinais = calcular_regras()

    votos_r = [
        regra
        for regra, sinal in sinais.items()
        if sinal == "R"
    ]

    votos_b = [
        regra
        for regra, sinal in sinais.items()
        if sinal == "B"
    ]

    qtd_r = len(votos_r)
    qtd_b = len(votos_b)

    # Strict majority:
    # empate nunca gera entrada.
    if qtd_r > qtd_b and qtd_r >= MIN_CONFLUENCIA:
        sinal = "R"
        votos = qtd_r
        regras = votos_r

    elif qtd_b > qtd_r and qtd_b >= MIN_CONFLUENCIA:
        sinal = "B"
        votos = qtd_b
        regras = votos_b

    else:
        sinal = None
        votos = max(qtd_r, qtd_b)
        regras = []

    return {
        "sinal": sinal,
        "votos": votos,
        "votos_r": votos_r,
        "votos_b": votos_b,
        "regras": regras,
        "sinais": sinais
    }


# ================================================================
# ESTADO / SESSÃO
# ================================================================

def start_bot():
    global bot_running
    global bot_state
    global signal_color
    global signal_issue
    global signal_votes
    global signal_rules
    global wins
    global losses
    global white_ignored
    global current_profit
    global history_results

    with state_lock:
        bot_running = True

        bot_state = "CACANDO"

        signal_color = None
        signal_issue = None
        signal_votes = 0
        signal_rules = []

        wins = 0
        losses = 0
        white_ignored = 0
        current_profit = 0.0

        history_results.clear()

        add_log("")
        add_log("==========================================")
        add_log("🟢 V3 INICIADO")
        add_log(
            f"🎯 Confluência mínima: {MIN_CONFLUENCIA} votos"
        )
        add_log(
            f"💰 Aposta simulada: R$ {APOSTA_BASE:.2f}"
        )
        add_log("⚪ WHITE será ignorado na avaliação.")
        add_log("==========================================")


def stop_bot():
    global bot_running
    global bot_state
    global signal_color
    global signal_issue
    global signal_votes
    global signal_rules

    with state_lock:
        bot_running = False

        bot_state = "PARADO"

        signal_color = None
        signal_issue = None
        signal_votes = 0
        signal_rules = []

        add_log("")
        add_log("==========================================")
        add_log("🔴 V3 PARADO")
        add_log(
            f"💰 Saldo da sessão: R$ {current_profit:.2f}"
        )
        add_log("📡 COLETA CONTINUA.")
        add_log("==========================================")


def zerar_sessao():
    global wins
    global losses
    global white_ignored
    global current_profit
    global history_results

    with state_lock:
        wins = 0
        losses = 0
        white_ignored = 0
        current_profit = 0.0
        history_results.clear()

        add_log("♻️ Placar da sessão zerado.")


# ================================================================
# PROCESSAMENTO DO RESULTADO
# ================================================================

def processar_resultado_novo(payload):
    global signal_color
    global signal_issue
    global signal_votes
    global signal_rules

    global wins
    global losses
    global white_ignored
    global current_profit

    rodada_id = str(payload.get("id"))
    roll = int(payload.get("roll"))
    color = int(payload.get("color"))

    sigla = {
        0: "W",
        1: "R",
        2: "B"
    }.get(color)

    if sigla is None:
        add_log(
            f"⚠️ Cor desconhecida | rodada={rodada_id}"
        )
        return

    # ============================================================
    # PRIMEIRO:
    # Se já existe sinal aberto, este resultado serve para
    # avaliá-lo.
    #
    # WHITE não encerra o sinal.
    # ============================================================

    with state_lock:

        if bot_running and bot_state == "ACOMPANHANDO":

            # ----------------------------------------------------
            # WHITE
            # ----------------------------------------------------
            if sigla == "W":

                white_ignored += 1

                history_results.append({
                    "issue": rodada_id,
                    "result": "WHITE",
                    "type": "WHITE IGNORADO",
                    "value": 0.0
                })

                add_log(
                    f"⚪ WHITE | {rodada_id} | "
                    f"roll={roll} | sinal {signal_color} continua."
                )

                salvar_operacao_db(
                    signal_issue,
                    rodada_id,
                    signal_color,
                    "WHITE",
                    roll,
                    signal_votes,
                    ",".join(signal_rules),
                    APOSTA_BASE,
                    0.0
                )

                # NÃO limpa o sinal.
                # O próximo RED/BLACK ainda será avaliado.
                return

            # ----------------------------------------------------
            # RED / BLACK
            # ----------------------------------------------------
            if sigla == signal_color:

                wins += 1
                current_profit += APOSTA_BASE

                history_results.append({
                    "issue": rodada_id,
                    "result": "WIN",
                    "type": "VITÓRIA",
                    "value": APOSTA_BASE
                })

                add_log(
                    f"✅ WIN | sinal {signal_color} | "
                    f"saiu {sigla} ({roll}) | "
                    f"+R$ {APOSTA_BASE:.2f}"
                )

                salvar_operacao_db(
                    signal_issue,
                    rodada_id,
                    signal_color,
                    "WIN",
                    roll,
                    signal_votes,
                    ",".join(signal_rules),
                    APOSTA_BASE,
                    APOSTA_BASE
                )

            else:

                losses += 1
                current_profit -= APOSTA_BASE

                history_results.append({
                    "issue": rodada_id,
                    "result": "LOSS",
                    "type": "DERROTA",
                    "value": -APOSTA_BASE
                })

                add_log(
                    f"❌ LOSS | sinal {signal_color} | "
                    f"saiu {sigla} ({roll}) | "
                    f"-R$ {APOSTA_BASE:.2f}"
                )

                salvar_operacao_db(
                    signal_issue,
                    rodada_id,
                    signal_color,
                    "LOSS",
                    roll,
                    signal_votes,
                    ",".join(signal_rules),
                    APOSTA_BASE,
                    -APOSTA_BASE
                )

            # ----------------------------------------------------
            # Depois de RED/BLACK, o sinal é encerrado.
            # ----------------------------------------------------
            signal_color = None
            signal_issue = None
            signal_votes = 0
            signal_rules = []

            bot_state = "CACANDO"

            return

        # ========================================================
        # SEM SINAL ABERTO
        # ========================================================

        if not bot_running:
            return

        # ========================================================
        # CAÇANDO:
        # calcula V3 usando SOMENTE o histórico anterior.
        # O resultado atual ainda não foi adicionado.
        # ========================================================

        if bot_state == "CACANDO":

            if len(history_numbers) < 2:
                return

            resultado = calcular_confluencia()

            sinais = resultado["sinais"]

            if sinais:
                partes = [
                    f"{regra}={sinal}"
                    for regra, sinal in sinais.items()
                ]

                add_log(
                    "🧠 " + " | ".join(partes)
                )

            if resultado["sinal"]:

                signal_color = resultado["sinal"]
                signal_issue = rodada_id
                signal_votes = resultado["votos"]
                signal_rules = resultado["regras"]

                bot_state = "ACOMPANHANDO"

                alvo = (
                    "🔴 RED"
                    if signal_color == "R"
                    else "⚫ BLACK"
                )

                add_log(
                    f"🚨 SINAL {alvo} | "
                    f"{signal_votes} VOTOS"
                )

                add_log(
                    f"📊 R={len(resultado['votos_r'])} | "
                    f"B={len(resultado['votos_b'])}"
                )

                add_log(
                    f"🧩 Regras: "
                    f"{', '.join(signal_rules)}"
                )

                add_log(
                    f"💰 Entrada simulada: "
                    f"R$ {APOSTA_BASE:.2f}"
                )

                # Não registra WIN/LOSS aqui.
                # O resultado seguinte será o avaliador.
                return

            else:

                if resultado["votos"]:
                    add_log(
                        f"⚪ SEM ENTRADA | "
                        f"R={len(resultado['votos_r'])} | "
                        f"B={len(resultado['votos_b'])}"
                    )


# ================================================================
# TICK SOCKET.IO
# ================================================================

def processar_tick(data):
    global total_ticks

    total_ticks += 1

    if not isinstance(data, dict):
        return

    event_id = data.get("id")
    payload = data.get("payload")

    if event_id != TICK_NAME:
        return

    if not isinstance(payload, dict):
        return

    rodada_id = payload.get("id")
    status = payload.get("status")
    color = payload.get("color")
    roll = payload.get("roll")

    if MOSTRAR_TICKS:
        cor = (
            nome_cor(color)
            if color is not None
            else "-"
        )

        add_log(
            f"📡 TICK | {rodada_id} | "
            f"{status} | {cor} | roll={roll}"
        )

    # Só interessa resultado final.
    if status != "complete":
        return

    if rodada_id is None or color is None or roll is None:
        return

    rodada_id = str(rodada_id)

    # Evita processar duas vezes na memória.
    with state_lock:
        if rodada_id in processed_issues:
            return

    # Primeiro grava no banco.
    inserido = salvar_resultado_db(payload)

    # Se já existia, não processa novamente.
    if not inserido:
        with state_lock:
            processed_issues.add(rodada_id)
        return

    with state_lock:
        processed_issues.add(rodada_id)

    # ============================================================
    # IMPORTANTE:
    # processar_resultado_novo usa o histórico ANTERIOR.
    # Só depois o resultado atual entra na memória.
    # ============================================================

    processar_resultado_novo(payload)

    with state_lock:
        history_numbers.append(int(roll))

        sigla = {
            0: "W",
            1: "R",
            2: "B"
        }.get(int(color))

        if sigla:
            history_colors.append(sigla)


# ================================================================
# SOCKET.IO
# ================================================================

def on_connect():
    global conectado

    conectado = True

    add_log("🟢 SOCKET.IO CONECTADO")
    add_log(
        f"📡 Room: {ROOM}"
    )

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

        add_log(
            f"📡 Subscribe enviado: {ROOM}"
        )

    except Exception as e:
        add_log(
            f"❌ Erro no subscribe: {str(e)[:180]}"
        )


def on_disconnect():
    global conectado

    conectado = False

    add_log(
        "🔴 SOCKET.IO DESCONECTADO"
    )


def on_connect_error(data):
    global conectado

    conectado = False

    add_log(
        f"⚠️ Socket.IO Connection error: {str(data)[:180]}"
    )


def on_data(data):
    try:
        processar_tick(data)

    except Exception as e:
        add_log(
            f"❌ Erro processando data: {str(e)[:200]}"
        )


def configurar_socket():
    global sio

    sio = socketio.Client(
        reconnection=True,
        reconnection_attempts=MAX_RECONEXOES,
        reconnection_delay=2,
        reconnection_delay_max=10,
        logger=False,
        engineio_logger=False
    )

    sio.on("connect", on_connect)
    sio.on("disconnect", on_disconnect)
    sio.on("connect_error", on_connect_error)
    sio.on(EVENT_NAME, on_data)


def iniciar_socket():
    configurar_socket()

    add_log("🔌 Conectando ao Blaze...")
    add_log(
        f"🌐 {BLAZE_URL}{SOCKET_PATH}"
    )

    try:
        sio.connect(
            BLAZE_URL,
            socketio_path=SOCKET_PATH,
            transports=["websocket"],
            wait_timeout=20
        )

        return True

    except Exception as e:
        add_log(
            f"❌ Falha inicial Socket.IO: {str(e)[:200]}"
        )
        return False


# ================================================================
# MONITOR
# ================================================================

def monitor_status():
    global rodando

    while rodando:

        time.sleep(INTERVALO_STATUS)

        if not rodando:
            break

        with state_lock:
            estado_socket = (
                "CONECTADO"
                if conectado
                else "DESCONECTADO"
            )

            estado_bot = bot_state

            saldo = current_profit

            resultados = total_resultados

            ultima = ultima_rodada

        add_log(
            f"📊 STATUS | "
            f"Socket={estado_socket} | "
            f"Coletados={resultados} | "
            f"Última={ultima or '-'} | "
            f"V3={estado_bot} | "
            f"Saldo=R$ {saldo:.2f}"
        )


# ================================================================
# SHUTDOWN
# ================================================================

def encerrar(sig=None, frame=None):
    global rodando
    global conectado

    if not rodando:
        return

    add_log("🛑 Encerrando aplicação...")

    rodando = False
    conectado = False

    try:
        if sio:
            sio.disconnect()
    except Exception:
        pass


signal.signal(signal.SIGTERM, encerrar)
signal.signal(signal.SIGINT, encerrar)


# ================================================================
# ROTAS WEB
# ================================================================

@app.route("/")
def home():

    with state_lock:

        total_operacoes = wins + losses

        if total_operacoes:
            win_rate = round(
                wins / total_operacoes * 100,
                1
            )
        else:
            win_rate = 0.0

        snapshot = {
            "running": bot_running,
            "state": bot_state,
            "signal": signal_color,
            "signal_votes": signal_votes,
            "signal_rules": list(signal_rules),
            "wins": wins,
            "losses": losses,
            "white": white_ignored,
            "profit": current_profit,
            "win_rate": win_rate,
            "connected": conectado,
            "last_round": ultima_rodada,
            "total_results": total_resultados,
            "total_ticks": total_ticks,
            "db_errors": total_erros_db,
            "numbers": list(history_numbers),
            "colors": list(history_colors),
            "logs": list(log_lines),
            "operations": list(history_results)
        }

    return render_template_string(
        HTML_TEMPLATE,
        **snapshot
    )


@app.route("/start", methods=["POST"])
def start():
    start_bot()
    return redirect("/")


@app.route("/stop", methods=["POST"])
def stop():
    stop_bot()
    return redirect("/")


@app.route("/reset", methods=["POST"])
def reset():
    zerar_sessao()
    return redirect("/")


@app.route("/health")
def health():
    with state_lock:
        return {
            "status": "ok",
            "socket_connected": conectado,
            "collector_results": total_resultados,
            "bot_running": bot_running
        }, 200


# ================================================================
# HTML
# ================================================================

HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="pt-br">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="10">
<title>Blaze Double V3</title>

<style>
:root {
    --bg: #07090d;
    --card: #11151c;
    --card2: #0d1117;
    --border: rgba(255,255,255,.08);
    --text: #e8edf3;
    --muted: #7d8794;
    --red: #ff4d57;
    --green: #19df78;
    --white: #f1f3f5;
    --yellow: #ffc247;
    --blue: #4da3ff;
}

* {
    box-sizing: border-box;
    margin: 0;
    padding: 0;
}

body {
    background:
        radial-gradient(circle at top, #171d28 0%, var(--bg) 52%);
    color: var(--text);
    font-family: Arial, sans-serif;
    min-height: 100vh;
    padding: 18px;
}

.container {
    max-width: 1180px;
    margin: auto;
}

.header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 15px;
    margin-bottom: 15px;
    flex-wrap: wrap;
}

.logo {
    font-size: 25px;
    font-weight: 900;
}

.logo span {
    color: var(--red);
}

.controls {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
}

.btn {
    border: 0;
    border-radius: 9px;
    padding: 10px 15px;
    font-weight: 800;
    cursor: pointer;
}

.btn-start {
    background: var(--green);
    color: #00150a;
}

.btn-stop {
    background: var(--red);
    color: white;
}

.btn-reset {
    background: #252b35;
    color: var(--text);
}

.status-grid {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 10px;
    margin-bottom: 12px;
}

.status-card,
.card {
    background: rgba(17,21,28,.86);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 15px;
}

.status-title,
.card-title {
    color: var(--muted);
    text-transform: uppercase;
    font-size: 10px;
    font-weight: 800;
    margin-bottom: 8px;
}

.status-value {
    font-size: 15px;
    font-weight: 900;
}

.online {
    color: var(--green);
}

.offline {
    color: var(--red);
}

.signal-box {
    margin-bottom: 12px;
    border-radius: 16px;
    padding: 20px;
    text-align: center;
    background: var(--card);
    border: 1px solid var(--border);
}

.signal-red {
    color: var(--red);
    font-size: 31px;
    font-weight: 1000;
}

.signal-black {
    color: #dce1e7;
    font-size: 31px;
    font-weight: 1000;
}

.signal-none {
    color: var(--yellow);
    font-size: 20px;
    font-weight: 900;
}

.signal-detail {
    margin-top: 7px;
    color: var(--muted);
    font-size: 13px;
}

.stats {
    display: grid;
    grid-template-columns: repeat(6, 1fr);
    gap: 10px;
    margin-bottom: 12px;
}

.value {
    font-size: 26px;
    font-weight: 950;
}

.green {
    color: var(--green);
}

.red {
    color: var(--red);
}

.yellow {
    color: var(--yellow);
}

.white {
    color: var(--white);
}

.profit-positive {
    color: var(--green);
}

.profit-negative {
    color: var(--red);
}

.trend {
    display: flex;
    gap: 6px;
    overflow-x: auto;
    padding: 10px;
    margin-bottom: 12px;
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 14px;
}

.pill {
    min-width: 38px;
    height: 38px;
    border-radius: 9px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-weight: 900;
    flex-shrink: 0;
}

.pill-r {
    background: var(--red);
    color: white;
}

.pill-b {
    background: #252a31;
    color: white;
    border: 1px solid #454c55;
}

.pill-w {
    background: white;
    color: #111;
}

.main-grid {
    display: grid;
    grid-template-columns: 1.7fr 1fr;
    gap: 12px;
}

.console,
.operations {
    height: 400px;
    overflow-y: auto;
    background: var(--card2);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 15px;
}

.console {
    font-family: Consolas, monospace;
}

.section-title {
    color: var(--muted);
    font-size: 11px;
    font-weight: 900;
    text-transform: uppercase;
    margin-bottom: 10px;
}

.log {
    padding: 7px 0;
    border-bottom: 1px solid rgba(255,255,255,.035);
    font-size: 13px;
    line-height: 1.35;
}

.log-win {
    color: var(--green);
    font-weight: 800;
}

.log-loss {
    color: var(--red);
    font-weight: 800;
}

.log-signal {
    color: #d7dde5;
    font-weight: 800;
}

.operation {
    display: grid;
    grid-template-columns: 1fr auto auto;
    gap: 8px;
    align-items: center;
    padding: 9px 0;
    border-bottom: 1px solid rgba(255,255,255,.04);
    font-size: 12px;
}

.issue {
    color: var(--muted);
    overflow: hidden;
    text-overflow: ellipsis;
}

.badge {
    padding: 4px 7px;
    border-radius: 6px;
    font-weight: 900;
}

.badge-win {
    background: rgba(25,223,120,.1);
    color: var(--green);
}

.badge-loss {
    background: rgba(255,77,87,.1);
    color: var(--red);
}

.badge-white {
    background: rgba(255,255,255,.08);
    color: white;
}

.footer {
    margin-top: 12px;
    color: var(--muted);
    font-size: 11px;
    text-align: center;
}

@media(max-width: 900px) {
    .status-grid {
        grid-template-columns: repeat(2, 1fr);
    }

    .stats {
        grid-template-columns: repeat(3, 1fr);
    }

    .main-grid {
        grid-template-columns: 1fr;
    }
}

@media(max-width: 600px) {
    body {
        padding: 10px;
    }

    .header {
        align-items: stretch;
    }

    .controls {
        width: 100%;
    }

    .btn {
        flex: 1;
    }

    .stats {
        grid-template-columns: repeat(2, 1fr);
    }

    .console,
    .operations {
        height: 300px;
    }
}
</style>
</head>

<body>

<div class="container">

<div class="header">
    <div class="logo">🤖 Blaze <span>Double V3</span></div>

    <div class="controls">
        {% if running %}
        <form method="POST" action="/stop">
            <button class="btn btn-stop">⏹ PARAR V3</button>
        </form>
        {% else %}
        <form method="POST" action="/start">
            <button class="btn btn-start">▶ INICIAR V3</button>
        </form>
        {% endif %}

        <form method="POST" action="/reset">
            <button class="btn btn-reset">↻ ZERAR</button>
        </form>
    </div>
</div>

<div class="status-grid">

    <div class="status-card">
        <div class="status-title">Socket Blaze</div>
        <div class="status-value {{ 'online' if connected else 'offline' }}">
            {{ '● CONECTADO' if connected else '● DESCONECTADO' }}
        </div>
    </div>

    <div class="status-card">
        <div class="status-title">Coletor</div>
        <div class="status-value online">
            {{ total_results }} resultados
        </div>
    </div>

    <div class="status-card">
        <div class="status-title">Última rodada</div>
        <div class="status-value">
            {{ last_round or '—' }}
        </div>
    </div>

    <div class="status-card">
        <div class="status-title">Motor V3</div>
        <div class="status-value">
            {% if running %}
                {% if state == 'ACOMPANHANDO' %}
                    <span class="red">🚨 ACOMPANHANDO</span>
                {% else %}
                    <span class="yellow">🎯 CAÇANDO</span>
                {% endif %}
            {% else %}
                <span class="offline">⏹ PARADO</span>
            {% endif %}
        </div>
    </div>

</div>

<div class="signal-box">

{% if running and state == 'ACOMPANHANDO' %}

    {% if signal == 'R' %}
        <div class="signal-red">🔴 ENTRAR RED</div>
    {% else %}
        <div class="signal-black">⚫ ENTRAR BLACK</div>
    {% endif %}

    <div class="signal-detail">
        {{ signal_votes }} votos |
        Regras: {{ signal_rules|join(', ') }} |
        Aposta simulada: R$ {{ '%.2f'|format(profit if false else 1.00) }}
    </div>

{% elif running %}

    <div class="signal-none">🎯 CAÇANDO SINAL V3</div>

    <div class="signal-detail">
        Mínimo de {{ min_confluencia }} votos.
        Empates não geram sinal.
    </div>

{% else %}

    <div class="signal-none">⏹ V3 PARADO</div>

    <div class="signal-detail">
        A coleta continua mesmo com o motor V3 parado.
    </div>

{% endif %}

</div>

<div class="trend">
    {% for i in range(numbers|length) %}
        <div class="pill pill-{{ colors[i].lower() }}">
            {{ numbers[i] }}
        </div>
    {% endfor %}
</div>

<div class="stats">

    <div class="card">
        <div class="card-title">Saldo</div>
        <div class="value {{ 'profit-positive' if profit >= 0 else 'profit-negative' }}">
            R$ {{ '%.2f'|format(profit) }}
        </div>
    </div>

    <div class="card">
        <div class="card-title">Wins</div>
        <div class="value green">{{ wins }}</div>
    </div>

    <div class="card">
        <div class="card-title">Losses</div>
        <div class="value red">{{ losses }}</div>
    </div>

    <div class="card">
        <div class="card-title">White</div>
        <div class="value white">{{ white }}</div>
    </div>

    <div class="card">
        <div class="card-title">Win rate</div>
        <div class="value yellow">{{ win_rate }}%</div>
    </div>

    <div class="card">
        <div class="card-title">Ticks</div>
        <div class="value">{{ total_ticks }}</div>
    </div>

</div>

<div class="main-grid">

    <div class="console">
        <div class="section-title">Console V3 / Coletor</div>

        {% for line in logs %}
            <div class="log
                {% if 'WIN' in line %}log-win
                {% elif 'LOSS' in line %}log-loss
                {% elif 'SINAL' in line or '🚨' in line %}log-signal
                {% endif %}">
                {{ line }}
            </div>
        {% endfor %}
    </div>

    <div class="operations">
        <div class="section-title">Histórico da sessão</div>

        {% if not operations %}
            <div style="color:#7d8794;text-align:center;padding:30px 5px;">
                Nenhuma operação nesta sessão.
            </div>
        {% endif %}

        {% for item in operations|reverse %}

            <div class="operation">

                <div class="issue">
                    {{ item.issue }}
                </div>

                {% if item.result == 'WIN' %}
                    <div class="badge badge-win">WIN</div>
                    <div class="green">
                        +R$ {{ '%.2f'|format(item.value) }}
                    </div>

                {% elif item.result == 'LOSS' %}
                    <div class="badge badge-loss">LOSS</div>
                    <div class="red">
                        -R$ {{ '%.2f'|format(-item.value) }}
                    </div>

                {% else %}
                    <div class="badge badge-white">WHITE</div>
                    <div>R$ 0,00</div>
                {% endif %}

            </div>

        {% endfor %}

    </div>

</div>

<div class="footer">
    Coleta contínua • PostgreSQL • Socket.IO • V3
    {% if db_errors > 0 %}
        • Erros DB: {{ db_errors }}
    {% endif %}
</div>

</div>

<script>
const consoleBox = document.querySelector(".console");
if (consoleBox) {
    consoleBox.scrollTop = consoleBox.scrollHeight;
}
</script>

</body>
</html>
"""


# ================================================================
# MAIN
# ================================================================

def bot_loop():

    add_log("🤖 Aplicação Blaze V3 iniciada.")
    add_log("📡 Coleta será mantida mesmo com V3 parado.")

    # O Socket.IO possui sua própria thread interna.
    # Aqui mantemos o processo vivo e monitorado.

    while rodando:
        time.sleep(1)


def main():

    add_log("==========================================")
    add_log("BLAZE DOUBLE — BOT V3")
    add_log("==========================================")

    if not os.environ.get("DATABASE_URL"):
        add_log("❌ DATABASE_URL não encontrada.")
        return

    if not init_db():
        add_log("❌ Banco não inicializado.")
        return

    # Recupera histórico antes de começar a operar.
    carregar_historico()

    # Começa parado.
    # O coletor continua ativo.
    global bot_running
    bot_running = False

    # Socket
    sucesso = iniciar_socket()

    if not sucesso:
        add_log(
            "⚠️ Conexão inicial falhou. "
            "O processo continuará tentando reconectar."
        )

    # Monitor
    thread_status = threading.Thread(
        target=monitor_status,
        daemon=True
    )

    thread_status.start()

    # Loop
    bot_loop()


# ================================================================
# EXECUÇÃO
# ================================================================

if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            "5000"
        )
    )

    thread_main = threading.Thread(
        target=main,
        daemon=True
    )

    thread_main.start()

    app.run(
        host="0.0.0.0",
        port=port
    )
