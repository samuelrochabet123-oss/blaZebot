import os
import time
import signal
import threading
from collections import deque
from datetime import datetime

import socketio
import psycopg2
import requests
from flask import Flask, redirect, render_template_string

# ================================================================
# BLAZE DOUBLE — BOT V3
# ================================================================
# Coleta contínua + PostgreSQL + motor V3 + painel web
#
# O bot NÃO executa apostas na Blaze.
# Ele apenas coleta resultados, gera sinais e contabiliza
# uma simulação de aposta fixa no painel.
#
# WHITE:
#   - permanece no histórico
#   - não é WIN
#   - não é LOSS
#   - não encerra sinal aberto
#
# V3:
#   - 10 regras
#   - mínimo operacional padrão = 2 votos
#   - empate = sem sinal
# ================================================================

# ================================================================
# CONFIGURAÇÃO
# ================================================================

BLAZE_URL = os.getenv("BLAZE_URL", "https://api-gaming.blaze.bet.br")
SOCKET_PATH = os.getenv("SOCKET_PATH", "/replication/")
ROOM = os.getenv("BLAZE_ROOM", "double_room_1")
EVENT_NAME = "data"
TICK_NAME = "double.tick"

MIN_CONFLUENCIA = int(os.getenv("MIN_CONFLUENCIA", "2"))
APOSTA_BASE = float(os.getenv("APOSTA_BASE", "1.00"))
HISTORICO_MEMORIA = int(os.getenv("HISTORICO_MEMORIA", "50"))
MAX_LOGS = 80
INTERVALO_STATUS = 30
MAX_RECONEXOES = 999999
MOSTRAR_TICKS = os.getenv("MOSTRAR_TICKS", "false").lower() == "true"
COLETA_MODO = os.getenv("COLETA_MODO", "postgres_bridge").lower().strip()
COLETA_DB_INTERVALO = float(os.getenv("COLETA_DB_INTERVALO", "1.0"))


# ================================================================
# CORES
# ================================================================

CORES = {
    0: "BRANCO",
    1: "VERMELHO",
    2: "PRETO",
}

COR_SIGLA = {
    "BRANCO": "W",
    "VERMELHO": "R",
    "PRETO": "B",
}


def nome_cor(cor):
    try:
        return CORES.get(int(cor), f"DESCONHECIDA({cor})")
    except Exception:
        return "DESCONHECIDA"


def sigla_cor(cor):
    return COR_SIGLA.get(nome_cor(cor))


def parse_timestamp(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


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

history_numbers = deque(maxlen=HISTORICO_MEMORIA)
history_colors = deque(maxlen=HISTORICO_MEMORIA)
processed_issues = set()
ultimo_id_banco_processado = 0

# ================================================================
# ESTADO DO BOT
# ================================================================

bot_running = False
bot_state = "PARADO"  # PARADO / CACANDO / ACOMPANHANDO

signal_color = None
signal_issue = None
signal_votes = 0
signal_rules = []

wins = 0
losses = 0
white_ignored = 0
current_profit = 0.0
history_results = deque(maxlen=30)

state_lock = threading.RLock()
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
        log_lines.append(f"[{timestamp}] {msg}")


# ================================================================
# POSTGRESQL
# ================================================================

def get_db_connection():
    database_url = os.environ.get("DATABASE_URL")

    if not database_url:
        add_log("⚠️ DATABASE_URL não configurada.")
        return None

    try:
        return psycopg2.connect(database_url, connect_timeout=15)
    except Exception as e:
        add_log(f"⚠️ Erro PostgreSQL: {str(e)[:180]}")
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
        add_log(f"❌ Erro inicializando banco: {e}")
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass
        return False


def salvar_resultado_db(payload):
    global total_resultados, total_duplicados, total_erros_db
    global ultima_rodada, ultimo_resultado_em

    rodada_id = payload.get("id")
    color = payload.get("color")
    roll = payload.get("roll")
    status = payload.get("status")
    room_id = payload.get("room_id")
    created_at = payload.get("created_at")
    updated_at = payload.get("updated_at")

    if not rodada_id or color is None or roll is None or status != "complete":
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
                    rodada_id, color, cor, roll, status, room_id,
                    created_at, updated_at
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (rodada_id) DO NOTHING
                RETURNING id;
            """, (
                str(rodada_id),
                int(color),
                cor_nome,
                int(roll),
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
            ultimo_resultado_em = datetime.now()
            add_log(
                f"💾 SALVO | {rodada_id} | {cor_nome} | roll={roll}"
            )
            return True

        total_duplicados += 1
        return False

    except Exception as e:
        total_erros_db += 1
        add_log(f"❌ Erro salvando resultado: {str(e)[:180]}")
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass
        return False


def carregar_historico():
    global ultimo_id_banco_processado
    """
    Carrega o histórico em ordem cronológica por created_at.
    Em caso de empate/nulo, usa coletado_em e id.
    """
    conn = get_db_connection()
    if not conn:
        return False

    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, rodada_id, roll, cor
                FROM blaze_historico
                WHERE status = 'complete'
                ORDER BY
                    COALESCE(created_at, coletado_em) ASC,
                    id ASC;
            """)
            rows = cur.fetchall()

            cur.execute("SELECT COALESCE(MAX(id), 0) FROM blaze_historico;")
            ultimo_id_banco_processado = int(cur.fetchone()[0] or 0)

        conn.close()

        with state_lock:
            history_numbers.clear()
            history_colors.clear()
            processed_issues.clear()

            for row_id, rodada_id, roll, cor in rows:
                if roll is None:
                    continue

                sigla = COR_SIGLA.get(str(cor).upper())
                if sigla is None:
                    continue

                history_numbers.append(int(roll))
                history_colors.append(sigla)
                processed_issues.add(str(rodada_id))

        add_log(f"📚 Histórico carregado: {len(history_numbers)} resultados em memória.")
        return True

    except Exception as e:
        add_log(f"⚠️ Erro carregando histórico: {e}")
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
    lucro,
):
    conn = get_db_connection()
    if not conn:
        return

    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO blaze_operacoes (
                    rodada_sinal, rodada_resultado, sinal, resultado,
                    roll, votos, regras, valor, lucro
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
                lucro,
            ))

        conn.commit()
        conn.close()

    except Exception as e:
        add_log(f"⚠️ Erro salvando operação: {str(e)[:160]}")
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
    Calcula as 10 regras usando SOMENTE o histórico anterior.
    Retorna apenas as regras que geraram sinal.
    """
    n = list(history_numbers)
    sinais = {}

    # R1 — SOMA2 <= 8 -> BLACK
    if len(n) >= 2 and n[-1] + n[-2] <= 8:
        sinais["R1"] = "B"

    # R2 — SOMA3 <= 14 -> RED
    if len(n) >= 3 and sum(n[-3:]) <= 14:
        sinais["R2"] = "R"

    # R3 — SOMA3 >= 30 -> BLACK
    if len(n) >= 3 and sum(n[-3:]) >= 30:
        sinais["R3"] = "B"

    # R4 — DISTÂNCIA >= 9 -> RED
    if len(n) >= 2 and abs(n[-1] - n[-2]) >= 9:
        sinais["R4"] = "R"

    # R5 — DISTÂNCIA >= 11 -> RED
    if len(n) >= 2 and abs(n[-1] - n[-2]) >= 11:
        sinais["R5"] = "R"

    # R6 — NÚMERO 3 -> RED
    if len(n) >= 1 and n[-1] == 3:
        sinais["R6"] = "R"

    # R7 — NÚMERO 4 -> BLACK
    if len(n) >= 1 and n[-1] == 4:
        sinais["R7"] = "B"

    # R8 — NÚMERO 6 -> BLACK
    if len(n) >= 1 and n[-1] == 6:
        sinais["R8"] = "B"

    # R9 — H10-H10-L7 -> BLACK
    if len(n) >= 3 and n[-3] >= 8 and n[-2] >= 8 and n[-1] == 7:
        sinais["R9"] = "B"

    # R10 — H7-H7-L1 -> BLACK
    if len(n) >= 3 and n[-3] >= 7 and n[-2] >= 7 and n[-1] <= 1:
        sinais["R10"] = "B"

    return sinais


def calcular_confluencia():
    sinais = calcular_regras()

    votos_r = [regra for regra, sinal in sinais.items() if sinal == "R"]
    votos_b = [regra for regra, sinal in sinais.items() if sinal == "B"]

    qtd_r = len(votos_r)
    qtd_b = len(votos_b)

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
        "sinais": sinais,
    }


# ================================================================
# ESTADO / SESSÃO
# ================================================================

def start_bot():
    global bot_running, bot_state
    global signal_color, signal_issue, signal_votes, signal_rules
    global wins, losses, white_ignored, current_profit
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

        add_log("==========================================")
        add_log("🟢 V3 INICIADO")
        add_log(f"🎯 Confluência mínima: {MIN_CONFLUENCIA} votos")
        add_log(f"💰 Aposta simulada: R$ {APOSTA_BASE:.2f}")
        add_log("⚪ WHITE será ignorado na avaliação.")
        add_log("==========================================")


def stop_bot():
    global bot_running, bot_state
    global signal_color, signal_issue, signal_votes, signal_rules

    with state_lock:
        bot_running = False
        bot_state = "PARADO"
        signal_color = None
        signal_issue = None
        signal_votes = 0
        signal_rules = []

        add_log("==========================================")
        add_log("🔴 V3 PARADO")
        add_log(f"💰 Saldo da sessão: R$ {current_profit:.2f}")
        add_log("📡 COLETA CONTINUA.")
        add_log("==========================================")


def zerar_sessao():
    global wins, losses, white_ignored, current_profit
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
    global signal_color, signal_issue, signal_votes, signal_rules
    global wins, losses, white_ignored, current_profit
    global bot_state

    rodada_id = str(payload.get("id"))
    roll = int(payload.get("roll"))
    color = int(payload.get("color"))

    sigla = {0: "W", 1: "R", 2: "B"}.get(color)
    if sigla is None:
        add_log(f"⚠️ Cor desconhecida | rodada={rodada_id}")
        return

    with state_lock:
        # ============================================================
        # SINAL ABERTO: o resultado atual é o avaliador.
        # WHITE não encerra o sinal.
        # ============================================================
        if bot_running and bot_state == "ACOMPANHANDO":
            if sigla == "W":
                white_ignored += 1
                history_results.append({
                    "issue": rodada_id,
                    "result": "WHITE",
                    "type": "WHITE IGNORADO",
                    "value": 0.0,
                })

                add_log(
                    f"⚪ WHITE | {rodada_id} | roll={roll} | "
                    f"sinal {signal_color} continua."
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
                    0.0,
                )
                return

            if sigla == signal_color:
                wins += 1
                current_profit += APOSTA_BASE
                history_results.append({
                    "issue": rodada_id,
                    "result": "WIN",
                    "type": "VITÓRIA",
                    "value": APOSTA_BASE,
                })

                add_log(
                    f"✅ WIN | sinal {signal_color} | saiu {sigla} ({roll}) | "
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
                    APOSTA_BASE,
                )
            else:
                losses += 1
                current_profit -= APOSTA_BASE
                history_results.append({
                    "issue": rodada_id,
                    "result": "LOSS",
                    "type": "DERROTA",
                    "value": -APOSTA_BASE,
                })

                add_log(
                    f"❌ LOSS | sinal {signal_color} | saiu {sigla} ({roll}) | "
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
                    -APOSTA_BASE,
                )

            signal_color = None
            signal_issue = None
            signal_votes = 0
            signal_rules = []
            bot_state = "CACANDO"
            return

        # ============================================================
        # SEM SINAL ABERTO
        # ============================================================
        if not bot_running:
            return

        if bot_state == "CACANDO":
            if len(history_numbers) < 2:
                return

            resultado = calcular_confluencia()
            sinais = resultado["sinais"]

            if sinais:
                partes = [f"{regra}={sinal}" for regra, sinal in sinais.items()]
                add_log("🧠 " + " | ".join(partes))

            if resultado["sinal"]:
                signal_color = resultado["sinal"]
                signal_issue = rodada_id
                signal_votes = resultado["votos"]
                signal_rules = resultado["regras"]
                bot_state = "ACOMPANHANDO"

                alvo = "🔴 RED" if signal_color == "R" else "⚫ BLACK"
                add_log(f"🚨 SINAL {alvo} | {signal_votes} VOTOS")
                add_log(
                    f"📊 R={len(resultado['votos_r'])} | "
                    f"B={len(resultado['votos_b'])}"
                )
                add_log(f"🧩 Regras: {', '.join(signal_rules)}")
                add_log(f"💰 Entrada simulada: R$ {APOSTA_BASE:.2f}")
            elif resultado["votos"]:
                add_log(
                    f"⚪ SEM ENTRADA | R={len(resultado['votos_r'])} | "
                    f"B={len(resultado['votos_b'])}"
                )


# ================================================================
# PONTE POSTGRESQL — CONSUMIR RESULTADOS DO COLAB
# ================================================================
#
# Quando a conexão direta Blaze -> Railway é bloqueada pelo ambiente
# do Railway, o Colab continua fazendo a coleta via Socket.IO e grava
# os resultados na MESMA tabela blaze_historico.
#
# O Railway passa então a fazer:
#
#   Colab -> Blaze/Socket.IO -> PostgreSQL
#                              ^
#                              |
#                    Railway lê daqui
#
# Isso elimina a necessidade de o Railway acessar a Blaze diretamente.
# ================================================================

def processar_resultado_banco(row):
    """Transforma uma linha já salva pelo coletor em um resultado do V3."""
    global ultimo_id_banco_processado, ultimo_resultado_em

    row_id, rodada_id, color, roll, status, room_id, created_at, updated_at = row

    if status != "complete" or rodada_id is None or color is None or roll is None:
        return False

    rodada_id = str(rodada_id)

    with state_lock:
        if rodada_id in processed_issues:
            return False

    payload = {
        "id": rodada_id,
        "color": int(color),
        "roll": int(roll),
        "status": status,
        "room_id": room_id,
        "created_at": created_at.isoformat() if hasattr(created_at, "isoformat") else created_at,
        "updated_at": updated_at.isoformat() if hasattr(updated_at, "isoformat") else updated_at,
    }

    # Aqui NÃO salvamos novamente no banco: o Colab já salvou.
    # Apenas alimentamos o mesmo motor V3 usado pelo Socket.IO.
    with state_lock:
        processed_issues.add(rodada_id)
        history_before = len(history_numbers)

    processar_resultado_novo(payload)

    with state_lock:
        history_numbers.append(int(roll))
        sigla = {0: "W", 1: "R", 2: "B"}.get(int(color))
        if sigla:
            history_colors.append(sigla)
        ultimo_resultado_em = datetime.now()
        ultima = rodada_id

    add_log(
        f"📥 DB BRIDGE | rodada={rodada_id} | {nome_cor(color)} | "
        f"roll={roll} | histórico_antes={history_before}"
    )
    return True


def monitorar_postgres():
    """Monitora a tabela e consome apenas linhas novas inseridas pelo Colab."""
    global ultimo_id_banco_processado, total_ticks, ultima_rodada
    global total_erros_db, conectado

    add_log("🟢 PONTE POSTGRESQL ATIVA")
    add_log(
        f"📡 Fonte: blaze_historico | intervalo={COLETA_DB_INTERVALO:.1f}s"
    )

    while rodando:
        conn = None
        try:
            conn = get_db_connection()
            if not conn:
                time.sleep(max(COLETA_DB_INTERVALO, 2.0))
                continue

            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, rodada_id, color, roll, status,
                           room_id, created_at, updated_at
                    FROM blaze_historico
                    WHERE id > %s
                      AND status = 'complete'
                      AND rodada_id IS NOT NULL
                      AND color IS NOT NULL
                      AND roll IS NOT NULL
                    ORDER BY id ASC;
                """, (ultimo_id_banco_processado,))
                rows = cur.fetchall()

            conn.close()
            conn = None

            with state_lock:
                conectado = True
                diagnostico["transporte_ativo"] = "POSTGRES_BRIDGE"

            for row in rows:
                row_id = int(row[0])
                total_ticks += 1
                processar_resultado_banco(row)
                ultimo_id_banco_processado = max(
                    ultimo_id_banco_processado,
                    row_id,
                )
                ultima_rodada = str(row[1])

        except Exception as e:
            total_erros_db += 1
            with state_lock:
                conectado = False
                diagnostico["transporte_ativo"] = "POSTGRES_BRIDGE_FALHA"
                diagnostico["ultimo_erro"] = repr(e)
            add_log(f"⚠️ DB BRIDGE | erro: {type(e).__name__}: {str(e)[:180]}")
            try:
                if conn:
                    conn.close()
            except Exception:
                pass

        time.sleep(max(COLETA_DB_INTERVALO, 0.2))


# ================================================================
# TICK SOCKET.IO
# ================================================================

def processar_tick(data):
    global total_ticks

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
        cor = nome_cor(color) if color is not None else "-"
        add_log(
            f"📡 TICK | {rodada_id} | {status} | {cor} | roll={roll}"
        )

    if status != "complete":
        return

    if rodada_id is None or color is None or roll is None:
        return

    rodada_id = str(rodada_id)

    with state_lock:
        if rodada_id in processed_issues:
            return

    # Grava primeiro. Só processa como novo se realmente foi inserido.
    inserido = salvar_resultado_db(payload)
    if not inserido:
        with state_lock:
            processed_issues.add(rodada_id)
        return

    with state_lock:
        processed_issues.add(rodada_id)

    # O motor usa o histórico ANTERIOR ao resultado atual.
    processar_resultado_novo(payload)

    # Só depois o resultado atual entra na memória do V3.
    with state_lock:
        history_numbers.append(int(roll))
        sigla = {0: "W", 1: "R", 2: "B"}.get(int(color))
        if sigla:
            history_colors.append(sigla)


# ================================================================
# SOCKET.IO — DIAGNÓSTICO
# ================================================================
#
# Esta versão NÃO altera as regras V3.
# Ela foi feita para descobrir exatamente em qual etapa a conexão
# com a Blaze está falhando no Railway.
#
# ETAPAS:
# 1) DNS
# 2) TCP 443
# 3) HTTPS básico
# 4) Engine.IO polling
# 5) Socket.IO WebSocket
#
# Se uma etapa funcionar e a seguinte falhar, o log mostrará onde.
# ================================================================

import socket as py_socket
import urllib.request
import urllib.error


diagnostico = {
    "dns": "NÃO TESTADO",
    "tcp": "NÃO TESTADO",
    "https": "NÃO TESTADO",
    "engineio": "NÃO TESTADO",
    "websocket": "NÃO TESTADO",
    "transporte_ativo": None,
    "ultimo_erro": None,
}


def diagnostico_dns():
    host = BLAZE_URL.replace("https://", "").replace("http://", "").split("/")[0]

    try:
        infos = py_socket.getaddrinfo(host, 443, type=py_socket.SOCK_STREAM)
        ips = sorted(set(info[4][0] for info in infos))

        diagnostico["dns"] = "OK"
        add_log(f"🔎 DNS OK | {host} -> {', '.join(ips[:5])}")
        return True

    except Exception as e:
        diagnostico["dns"] = "FALHA"
        diagnostico["ultimo_erro"] = repr(e)
        add_log(f"❌ DNS FALHOU | {host} | {repr(e)}")
        return False


def diagnostico_tcp():
    host = BLAZE_URL.replace("https://", "").replace("http://", "").split("/")[0]

    try:
        inicio = time.time()

        sock = py_socket.create_connection(
            (host, 443),
            timeout=10
        )
        sock.close()

        ms = round((time.time() - inicio) * 1000, 1)

        diagnostico["tcp"] = "OK"
        add_log(f"🔎 TCP 443 OK | {host}:443 | {ms} ms")
        return True

    except Exception as e:
        diagnostico["tcp"] = "FALHA"
        diagnostico["ultimo_erro"] = repr(e)
        add_log(f"❌ TCP 443 FALHOU | {host}:443 | {repr(e)}")
        return False


def diagnostico_https():
    url = BLAZE_URL.rstrip("/") + SOCKET_PATH

    # O objetivo aqui NÃO é esperar um resultado de jogo.
    # É apenas confirmar que o Railway consegue fazer HTTPS
    # até o endpoint informado.
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "BlazeDoubleV3-Diagnostic/1.0",
                "Accept": "*/*",
            },
            method="GET",
        )

        inicio = time.time()

        with urllib.request.urlopen(req, timeout=15) as response:
            status = response.status
            body = response.read(500).decode(
                "utf-8",
                errors="replace"
            )

        ms = round((time.time() - inicio) * 1000, 1)

        diagnostico["https"] = f"HTTP {status}"

        add_log(
            f"🔎 HTTPS OK | HTTP {status} | {ms} ms | "
            f"body={body[:180]!r}"
        )

        return True

    except urllib.error.HTTPError as e:
        body = ""

        try:
            body = e.read(500).decode(
                "utf-8",
                errors="replace"
            )
        except Exception:
            pass

        diagnostico["https"] = f"HTTP {e.code}"
        diagnostico["ultimo_erro"] = repr(e)

        add_log(
            f"⚠️ HTTPS respondeu | HTTP {e.code} | "
            f"body={body[:180]!r}"
        )

        # HTTP 4xx/5xx prova que houve comunicação HTTPS.
        return True

    except Exception as e:
        diagnostico["https"] = "FALHA"
        diagnostico["ultimo_erro"] = repr(e)
        add_log(f"❌ HTTPS FALHOU | {repr(e)}")
        return False


def diagnostico_engineio_polling():
    """
    Teste direto do handshake Engine.IO.

    Isso é diferente do Socket.IO completo.
    Se funcionar, sabemos que o endpoint /replication/ está
    respondendo ao protocolo Engine.IO no Railway.
    """
    url = (
        BLAZE_URL.rstrip("/")
        + SOCKET_PATH
        + "?EIO=4&transport=polling"
    )

    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "BlazeDoubleV3-Diagnostic/1.0",
                "Accept": "*/*",
            },
            method="GET",
        )

        inicio = time.time()

        with urllib.request.urlopen(req, timeout=15) as response:
            status = response.status
            body = response.read(1000).decode(
                "utf-8",
                errors="replace"
            )

        ms = round((time.time() - inicio) * 1000, 1)

        diagnostico["engineio"] = f"HTTP {status}"

        add_log(
            f"🔎 ENGINE.IO POLLING | HTTP {status} | "
            f"{ms} ms | resposta={body[:300]!r}"
        )

        return True

    except urllib.error.HTTPError as e:
        body = ""

        try:
            body = e.read(1000).decode(
                "utf-8",
                errors="replace"
            )
        except Exception:
            pass

        diagnostico["engineio"] = f"HTTP {e.code}"
        diagnostico["ultimo_erro"] = repr(e)

        add_log(
            f"❌ ENGINE.IO POLLING | HTTP {e.code} | "
            f"resposta={body[:300]!r}"
        )

        return False

    except Exception as e:
        diagnostico["engineio"] = "FALHA"
        diagnostico["ultimo_erro"] = repr(e)
        add_log(f"❌ ENGINE.IO POLLING FALHOU | {repr(e)}")
        return False


def on_connect():
    global conectado

    conectado = True
    diagnostico["transporte_ativo"] = "CONECTADO"

    add_log("🟢 SOCKET.IO CONECTADO")
    add_log(f"📡 Room: {ROOM}")

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
            f"❌ ERRO NO SUBSCRIBE | {repr(e)}"
        )


def on_disconnect():
    global conectado

    conectado = False
    add_log("🔴 SOCKET.IO DESCONECTADO")


def on_connect_error(data):
    global conectado

    conectado = False

    diagnostico["ultimo_erro"] = repr(data)

    add_log(
        "❌ SOCKET.IO CONNECTION ERROR"
    )
    add_log(
        f"   Tipo: {type(data).__name__}"
    )
    add_log(
        f"   Detalhe: {repr(data)}"
    )


def on_data(data):
    try:
        processar_tick(data)

    except Exception as e:
        add_log(
            f"❌ Erro processando data: {repr(e)}"
        )


# Headers usados somente como compatibilidade com servidores/proxies que
# esperam uma origem semelhante a um navegador. Eles podem ser alterados
# por variáveis de ambiente sem editar o código.
BLAZE_USER_AGENT = os.getenv(
    "BLAZE_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
BLAZE_ORIGIN = os.getenv("BLAZE_ORIGIN", "https://blaze.bet.br")
BLAZE_REFERER = os.getenv("BLAZE_REFERER", "https://blaze.bet.br/")


def _headers_compatibilidade():
    return {
        "User-Agent": BLAZE_USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Origin": BLAZE_ORIGIN,
        "Referer": BLAZE_REFERER,
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }


def _criar_sessao_browser():
    """Cria uma sessão HTTP persistente e tenta obter cookies iniciais do site."""
    headers = _headers_compatibilidade()
    sessao = requests.Session()
    sessao.headers.update(headers)

    add_log("🌐 Aquecendo sessão HTTP do site Blaze...")

    try:
        inicio = time.time()
        resposta = sessao.get(
            BLAZE_ORIGIN.rstrip("/") + "/",
            timeout=15,
            allow_redirects=True,
        )
        ms = round((time.time() - inicio) * 1000, 1)

        add_log(
            f"🌐 SITE BLAZE | HTTP {resposta.status_code} | {ms} ms | "
            f"url_final={resposta.url}"
        )
        add_log(
            f"🍪 Cookies obtidos: {len(sessao.cookies)}"
        )

        if resposta.status_code == 403:
            add_log(
                "🚫 O próprio site Blaze respondeu 403 para o Railway. "
                "Isso aponta para bloqueio de origem/IP ou desafio do Cloudflare."
            )
        elif resposta.status_code < 400:
            add_log("🟢 Sessão HTTP inicial aceita pelo site Blaze.")
        else:
            add_log(
                f"⚠️ Site Blaze respondeu HTTP {resposta.status_code}; "
                "a sessão ainda será usada no teste Socket.IO."
            )

    except Exception as e:
        add_log(f"⚠️ Falha ao aquecer sessão HTTP: {type(e).__name__}: {repr(e)}")

    return sessao


def configurar_socket(transporte, headers=None, http_session=None):
    global sio

    sio = socketio.Client(
        reconnection=True,
        reconnection_attempts=MAX_RECONEXOES,
        reconnection_delay=2,
        reconnection_delay_max=10,
        logger=False,
        engineio_logger=False,
        http_session=http_session,
    )

    sio.on("connect", on_connect)
    sio.on("disconnect", on_disconnect)
    sio.on("connect_error", on_connect_error)
    sio.on(EVENT_NAME, on_data)

    add_log(f"⚙️ Socket configurado | transporte={transporte}")
    if headers:
        add_log("🧩 Headers de compatibilidade ativados")
    if http_session is not None:
        add_log("🍪 Sessão HTTP persistente vinculada ao Socket.IO")


def tentar_socket(transporte):
    global sio

    tipo_diag = "websocket" if transporte == "websocket" else "engineio"

    add_log("")
    add_log("================================================")
    add_log(f"🔌 INICIANDO CONEXÃO | transporte={transporte}")
    add_log(f"🌐 URL={BLAZE_URL}")
    add_log(f"🛣️ PATH={SOCKET_PATH}")
    add_log(f"🏠 ROOM={ROOM}")
    add_log("================================================")

    try:
        from importlib.metadata import version
        add_log(
            "📦 VERSÕES | "
            f"python-socketio={version('python-socketio')} | "
            f"python-engineio={version('python-engineio')} | "
            f"websocket-client={version('websocket-client')} | "
            f"requests={version('requests')}"
        )
    except Exception as e:
        add_log(f"⚠️ Não foi possível obter versões dos pacotes: {repr(e)}")

    # Primeira tentativa: padrão do Collector.
    tentativas = [
        ("collector", None, None),
        ("browser", _headers_compatibilidade(), _criar_sessao_browser()),
    ]

    for indice, (modo, headers, http_session) in enumerate(tentativas, start=1):
        configurar_socket(
            transporte,
            headers=headers,
            http_session=http_session,
        )

        try:
            add_log(
                f"⏳ Tentativa {indice}/2 | modo={modo} | "
                f"transport={transporte}"
            )

            if headers:
                add_log(f"🌍 Origin={headers['Origin']}")
                add_log(f"🧭 Referer={headers['Referer']}")
                add_log(f"🖥️ User-Agent={headers['User-Agent'][:120]}")

            if http_session is not None:
                add_log(
                    f"🍪 Cookies antes do Socket.IO: "
                    f"{len(http_session.cookies)}"
                )

            inicio = time.time()

            kwargs = {
                "socketio_path": SOCKET_PATH,
                "transports": [transporte],
                "wait_timeout": 20,
            }

            if headers:
                kwargs["headers"] = headers

            add_log("➡️ Chamando sio.connect()...")
            sio.connect(BLAZE_URL, **kwargs)

            ms = round((time.time() - inicio) * 1000, 1)
            conectado_socket = getattr(sio, "connected", False)

            add_log(
                f"🟢 sio.connect() retornou | {ms} ms | "
                f"connected={conectado_socket}"
            )

            if conectado_socket:
                diagnostico[tipo_diag] = "OK"
                diagnostico["ultimo_erro"] = ""
                add_log(
                    f"🟢 CONEXÃO SOCKET.IO OK | transporte={transporte} | "
                    f"modo={modo}"
                )
                return True

            add_log("⚠️ sio.connect() retornou, mas connected=False")

        except Exception as e:
            diagnostico[tipo_diag] = "FALHA"
            diagnostico["ultimo_erro"] = repr(e)

            add_log(f"❌ FALHA | modo={modo} | transporte={transporte}")
            add_log(f"❌ Tipo: {type(e).__name__}")
            add_log(f"❌ Erro: {repr(e)}")

            if "403" in repr(e):
                add_log("🚫 HTTP 403 detectado pelo servidor/Cloudflare")
                add_log("   A conexão foi recusada antes do subscribe da sala.")

        finally:
            try:
                if sio and getattr(sio, "connected", False):
                    sio.disconnect()
            except Exception:
                pass

        if indice < len(tentativas):
            time.sleep(1)

    add_log(f"🔴 TESTE FINALIZADO COM FALHA | transporte={transporte}")
    return False


def iniciar_socket():
    add_log("================================================")
    add_log("🔬 DIAGNÓSTICO DE CONEXÃO BLAZE")
    add_log("================================================")
    add_log(f"🌐 URL: {BLAZE_URL}")
    add_log(f"🛣️ SOCKET PATH: {SOCKET_PATH}")
    add_log(f"🏠 ROOM: {ROOM}")
    add_log("")

    # ------------------------------------------------------------
    # 1. DNS
    # ------------------------------------------------------------
    add_log("1️⃣ TESTE DNS")
    if not diagnostico_dns():
        add_log("⛔ Diagnóstico interrompido: DNS não resolveu.")
        return False

    # ------------------------------------------------------------
    # 2. TCP 443
    # ------------------------------------------------------------
    add_log("2️⃣ TESTE TCP 443")
    if not diagnostico_tcp():
        add_log("⛔ Diagnóstico interrompido: TCP 443 inacessível.")
        return False

    # ------------------------------------------------------------
    # 3. HTTPS
    # ------------------------------------------------------------
    add_log("3️⃣ TESTE HTTPS")
    diagnostico_https()

    # ------------------------------------------------------------
    # 4. Engine.IO polling
    # ------------------------------------------------------------
    add_log("4️⃣ TESTE ENGINE.IO POLLING")
    polling_http_ok = diagnostico_engineio_polling()

    # ------------------------------------------------------------
    # 5. Socket.IO WebSocket
    # ------------------------------------------------------------
    add_log("5️⃣ TESTE SOCKET.IO WEBSOCKET")

    if tentar_socket("websocket"):
        add_log("================================================")
        add_log("✅ DIAGNÓSTICO: WEBSOCKET FUNCIONOU")
        add_log("================================================")
        return True

    # ------------------------------------------------------------
    # 6. Fallback diagnóstico: polling
    # ------------------------------------------------------------
    add_log("================================================")
    add_log("⚠️ WEBSOCKET FALHOU")
    add_log("🔄 Testando Socket.IO via POLLING...")
    add_log("================================================")

    if tentar_socket("polling"):
        add_log("================================================")
        add_log("✅ SOCKET.IO POLLING FUNCIONOU")
        add_log("⚠️ O problema está especificamente no WEBSOCKET.")
        add_log("================================================")
        return True

    add_log("================================================")
    add_log("❌ DIAGNÓSTICO: SOCKET.IO NÃO CONECTOU")
    add_log("================================================")

    add_log(
        f"DNS={diagnostico['dns']} | "
        f"TCP={diagnostico['tcp']} | "
        f"HTTPS={diagnostico['https']} | "
        f"EngineIO={diagnostico['engineio']} | "
        f"WebSocket={diagnostico['websocket']}"
    )

    add_log(
        f"Último erro: {diagnostico['ultimo_erro']}"
    )

    if polling_http_ok:
        add_log(
            "ℹ️ O endpoint Engine.IO respondeu via HTTP, "
            "mas o cliente Socket.IO não conseguiu completar a conexão."
        )

    return False


# ================================================================
# MONITOR / SHUTDOWN
# ================================================================

def monitor_status():
    global rodando

    while rodando:
        time.sleep(INTERVALO_STATUS)
        if not rodando:
            break

        with state_lock:
            estado_socket = "CONECTADO" if conectado else "DESCONECTADO"
            estado_bot = bot_state
            saldo = current_profit
            resultados = total_resultados
            ultima = ultima_rodada

        add_log(
            f"📊 STATUS | Socket={estado_socket} | "
            f"Coletados={resultados} | Última={ultima or '-'} | "
            f"V3={estado_bot} | Saldo=R$ {saldo:.2f}"
        )


def encerrar(sig=None, frame=None):
    global rodando, conectado

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
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
    background: radial-gradient(circle at top, #171d28 0%, var(--bg) 52%);
    color: var(--text);
    font-family: Arial, sans-serif;
    min-height: 100vh;
    padding: 18px;
}
.container { max-width: 1180px; margin: auto; }
.header {
    display: flex; justify-content: space-between; align-items: center;
    gap: 15px; margin-bottom: 15px; flex-wrap: wrap;
}
.logo { font-size: 25px; font-weight: 900; }
.logo span { color: var(--red); }
.controls { display: flex; gap: 8px; flex-wrap: wrap; }
.btn {
    border: 0; border-radius: 9px; padding: 10px 15px;
    font-weight: 800; cursor: pointer;
}
.btn-start { background: var(--green); color: #00150a; }
.btn-stop { background: var(--red); color: white; }
.btn-reset { background: #252b35; color: var(--text); }
.status-grid {
    display: grid; grid-template-columns: repeat(4, 1fr);
    gap: 10px; margin-bottom: 12px;
}
.status-card, .card {
    background: rgba(17,21,28,.86); border: 1px solid var(--border);
    border-radius: 14px; padding: 15px;
}
.status-title, .card-title {
    color: var(--muted); text-transform: uppercase;
    font-size: 10px; font-weight: 800; margin-bottom: 8px;
}
.status-value { font-size: 15px; font-weight: 900; }
.online { color: var(--green); }
.offline { color: var(--red); }
.signal-box {
    margin-bottom: 12px; border-radius: 16px; padding: 20px;
    text-align: center; background: var(--card); border: 1px solid var(--border);
}
.signal-red { color: var(--red); font-size: 31px; font-weight: 1000; }
.signal-black { color: #dce1e7; font-size: 31px; font-weight: 1000; }
.signal-none { color: var(--yellow); font-size: 20px; font-weight: 900; }
.signal-detail { margin-top: 7px; color: var(--muted); font-size: 13px; }
.stats {
    display: grid; grid-template-columns: repeat(6, 1fr);
    gap: 10px; margin-bottom: 12px;
}
.value { font-size: 26px; font-weight: 950; }
.green { color: var(--green); }
.red { color: var(--red); }
.yellow { color: var(--yellow); }
.white { color: var(--white); }
.profit-positive { color: var(--green); }
.profit-negative { color: var(--red); }
.trend {
    display: flex; gap: 6px; overflow-x: auto; padding: 10px;
    margin-bottom: 12px; background: var(--card);
    border: 1px solid var(--border); border-radius: 14px;
}
.pill {
    min-width: 38px; height: 38px; border-radius: 9px;
    display: flex; align-items: center; justify-content: center;
    font-weight: 900; flex-shrink: 0;
}
.pill-r { background: var(--red); color: white; }
.pill-b { background: #252a31; color: white; border: 1px solid #454c55; }
.pill-w { background: white; color: #111; }
.main-grid { display: grid; grid-template-columns: 1.7fr 1fr; gap: 12px; }
.console, .operations {
    height: 400px; overflow-y: auto; background: var(--card2);
    border: 1px solid var(--border); border-radius: 14px; padding: 15px;
}
.console { font-family: Consolas, monospace; }
.section-title {
    color: var(--muted); font-size: 11px; font-weight: 900;
    text-transform: uppercase; margin-bottom: 10px;
}
.log { padding: 7px 0; border-bottom: 1px solid rgba(255,255,255,.035); font-size: 13px; line-height: 1.35; }
.log-win { color: var(--green); font-weight: 800; }
.log-loss { color: var(--red); font-weight: 800; }
.log-signal { color: #d7dde5; font-weight: 800; }
.operation {
    display: grid; grid-template-columns: 1fr auto auto; gap: 8px;
    align-items: center; padding: 9px 0;
    border-bottom: 1px solid rgba(255,255,255,.04); font-size: 12px;
}
.issue { color: var(--muted); overflow: hidden; text-overflow: ellipsis; }
.badge { padding: 4px 7px; border-radius: 6px; font-weight: 900; }
.badge-win { background: rgba(25,223,120,.1); color: var(--green); }
.badge-loss { background: rgba(255,77,87,.1); color: var(--red); }
.badge-white { background: rgba(255,255,255,.08); color: white; }
.footer { margin-top: 12px; color: var(--muted); font-size: 11px; text-align: center; }
@media(max-width: 900px) {
    .status-grid { grid-template-columns: repeat(2, 1fr); }
    .stats { grid-template-columns: repeat(3, 1fr); }
    .main-grid { grid-template-columns: 1fr; }
}
@media(max-width: 600px) {
    body { padding: 10px; }
    .header { align-items: stretch; }
    .controls { width: 100%; }
    .btn { flex: 1; }
    .stats { grid-template-columns: repeat(2, 1fr); }
    .console, .operations { height: 300px; }
}
</style>
</head>
<body>
<div class="container">

<div class="header">
    <div class="logo">🤖 Blaze <span>Double V3</span></div>
    <div class="controls">
        {% if running %}
        <form method="POST" action="/stop"><button class="btn btn-stop">⏹ PARAR V3</button></form>
        {% else %}
        <form method="POST" action="/start"><button class="btn btn-start">▶ INICIAR V3</button></form>
        {% endif %}
        <form method="POST" action="/reset"><button class="btn btn-reset">↻ ZERAR</button></form>
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
        <div class="status-value online">{{ total_results }} resultados</div>
    </div>
    <div class="status-card">
        <div class="status-title">Última rodada</div>
        <div class="status-value">{{ last_round or '—' }}</div>
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
        Aposta simulada: R$ {{ '%.2f'|format(bet_amount) }}
    </div>
{% elif running %}
    <div class="signal-none">🎯 CAÇANDO SINAL V3</div>
    <div class="signal-detail">
        Mínimo de {{ min_confluencia }} votos. Empates não geram sinal.
    </div>
{% else %}
    <div class="signal-none">⏹ V3 PARADO</div>
    <div class="signal-detail">A coleta continua mesmo com o motor V3 parado.</div>
{% endif %}
</div>

<div class="stats">
    <div class="card">
        <div class="card-title">Saldo</div>
        <div class="value {{ 'profit-positive' if profit >= 0 else 'profit-negative' }}">
            R$ {{ '%.2f'|format(profit) }}
        </div>
    </div>
    <div class="card"><div class="card-title">Wins</div><div class="value green">{{ wins }}</div></div>
    <div class="card"><div class="card-title">Losses</div><div class="value red">{{ losses }}</div></div>
    <div class="card"><div class="card-title">White</div><div class="value white">{{ white }}</div></div>
    <div class="card"><div class="card-title">Win rate</div><div class="value yellow">{{ win_rate }}%</div></div>
    <div class="card"><div class="card-title">Ticks</div><div class="value">{{ total_ticks }}</div></div>
</div>

<div class="trend">
    {% for i in range(numbers|length) %}
        <div class="pill pill-{{ colors[i].lower() }}">{{ numbers[i] }}</div>
    {% endfor %}
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
            <div style="color:#7d8794;text-align:center;padding:30px 5px;">Nenhuma operação nesta sessão.</div>
        {% endif %}
        {% for item in operations|reverse %}
            <div class="operation">
                <div class="issue">{{ item.issue }}</div>
                {% if item.result == 'WIN' %}
                    <div class="badge badge-win">WIN</div>
                    <div class="green">+R$ {{ '%.2f'|format(item.value) }}</div>
                {% elif item.result == 'LOSS' %}
                    <div class="badge badge-loss">LOSS</div>
                    <div class="red">-R$ {{ '%.2f'|format(-item.value) }}</div>
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
    {% if db_errors > 0 %} • Erros DB: {{ db_errors }}{% endif %}
</div>

</div>
<script>
const consoleBox = document.querySelector('.console');
if (consoleBox) consoleBox.scrollTop = consoleBox.scrollHeight;
</script>
</body>
</html>
"""


# ================================================================
# ROTAS WEB
# ================================================================

@app.route("/")
def home():
    with state_lock:
        total_operacoes = wins + losses
        win_rate = round(wins / total_operacoes * 100, 1) if total_operacoes else 0.0

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
            "operations": list(history_results),
            "min_confluencia": MIN_CONFLUENCIA,
            "bet_amount": APOSTA_BASE,
        }

    return render_template_string(HTML_TEMPLATE, **snapshot)


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
            "bot_running": bot_running,
            "diagnostic": dict(diagnostico),
        }, 200


# ================================================================
# MAIN
# ================================================================

def bot_loop():
    add_log("🤖 Aplicação Blaze V3 iniciada.")
    add_log("📡 Coleta será mantida mesmo com V3 parado.")

    while rodando:
        time.sleep(1)


def main():
    add_log("==========================================")
    add_log("BLAZE DOUBLE — BOT V3 / DIAGNÓSTICO")
    add_log("==========================================")

    if not os.environ.get("DATABASE_URL"):
        add_log("❌ DATABASE_URL não encontrada.")
        return

    if not init_db():
        add_log("❌ Banco não inicializado.")
        return

    carregar_historico()

    global bot_running
    bot_running = False

    add_log(f"🧭 MODO DE COLETA: {COLETA_MODO}")

    if COLETA_MODO in ("postgres", "postgres_bridge", "bridge"):
        add_log("🔗 Railway não acessará a Blaze diretamente.")
        add_log("📥 O Railway consumirá os resultados gravados pelo Colab.")
        thread_coleta = threading.Thread(
            target=monitorar_postgres,
            daemon=True,
            name="postgres-bridge",
        )
        thread_coleta.start()
    else:
        sucesso = iniciar_socket()
        if not sucesso:
            add_log(
                "⚠️ Conexão inicial falhou. "
                "O processo continuará tentando reconectar."
            )

    thread_status = threading.Thread(target=monitor_status, daemon=True)
    thread_status.start()

    bot_loop()


# ================================================================
# EXECUÇÃO
# ================================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))

    thread_main = threading.Thread(target=main, daemon=True)
    thread_main.start()

    app.run(host="0.0.0.0", port=port)
