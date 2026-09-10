import os
import time
import threading
from datetime import datetime

import psycopg2
import socketio
from flask import Flask, redirect, render_template_string

# ==============================================================================
# BLAZE DOUBLE V3 — BOT ONLINE
# ==============================================================================

BLAZE_SOCKET_URL = "https://api-gaming.blaze.bet.br"
SOCKETIO_PATH = "/replication/"
ROOM = "double_room_1"

# O token NÃO fica no código. Configure BLAZE_TOKEN na Railway.
BLAZE_TOKEN = os.getenv("BLAZE_TOKEN", "").strip()

# V3 validada no laboratório.
MIN_CONFLUENCIA = int(os.getenv("MIN_CONFLUENCIA", "2"))
APOSTA_BASE = float(os.getenv("APOSTA_BASE", "1.00"))

# Memória operacional.
HISTORY_LIMIT = 60

# ==============================================================================
# ESTADO GLOBAL
# ==============================================================================

history_numbers = []
history_colors = []
history_rounds = []
processed_rounds = set()

bot_running = False
bot_state = "PARADO"

signal_color = None
signal_votes = 0
signal_reason = ""
signal_round = None

wins = 0
losses = 0
current_profit = 0.0

operations = []
logs = []

state_lock = threading.RLock()

sio = socketio.Client(
    reconnection=True,
    reconnection_attempts=0,
    logger=False,
    engineio_logger=False
)

app = Flask(__name__)


# ==============================================================================
# LOG
# ==============================================================================

def add_log(msg):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"

    with state_lock:
        logs.append(line)

        if len(logs) > 100:
            del logs[:-100]

    print(line, flush=True)


# ==============================================================================
# BANCO
# ==============================================================================

def get_db():
    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        return None

    return psycopg2.connect(
        database_url,
        connect_timeout=8
    )


def init_db():
    conn = get_db()

    if not conn:
        add_log("⚠️ DATABASE_URL não configurada.")
        return

    try:
        with conn:
            with conn.cursor() as cur:

                cur.execute("""
                    CREATE TABLE IF NOT EXISTS blaze_operacoes (
                        id BIGSERIAL PRIMARY KEY,
                        rodada_sinal TEXT,
                        rodada_resultado TEXT,
                        sinal TEXT NOT NULL,
                        resultado TEXT NOT NULL,
                        numero INTEGER,
                        votos_r INTEGER DEFAULT 0,
                        votos_b INTEGER DEFAULT 0,
                        confluencia INTEGER DEFAULT 0,
                        valor REAL DEFAULT 1.0,
                        lucro REAL DEFAULT 0.0,
                        criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)

        add_log("🗄️ Banco operacional pronto.")

    except Exception as e:
        add_log(f"⚠️ Erro inicializando banco: {e}")

    finally:
        conn.close()


def load_history():
    """
    Carrega o histórico existente.

    IMPORTANTE:
    O bot usa a ordem do coletor:
        coletado_em ASC, id ASC

    Isso deve ser validado contra a ordem real das rodadas antes de
    tratar o backtest como evidência definitiva.
    """

    conn = get_db()

    if not conn:
        return

    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    rodada_id,
                    roll,
                    cor
                FROM blaze_historico
                WHERE roll IS NOT NULL
                  AND cor IS NOT NULL
                ORDER BY
                    coletado_em ASC NULLS LAST,
                    id ASC
            """)

            rows = cur.fetchall()

        rows = rows[-HISTORY_LIMIT:]

        with state_lock:
            history_rounds[:] = [str(row[0]) for row in rows]
            history_numbers[:] = [int(row[1]) for row in rows]
            history_colors[:] = [str(row[2]).upper() for row in rows]

            processed_rounds.update(history_rounds)

        add_log(
            f"📚 Histórico carregado: "
            f"{len(rows)} resultados."
        )

    except Exception as e:
        add_log(f"⚠️ Erro carregando histórico: {e}")

    finally:
        conn.close()


def save_result(round_id, number, color):
    """
    Salva na tabela existente blaze_historico.

    A tabela do coletor já possui UNIQUE/PRIMARY KEY em rodada_id.
    """

    conn = get_db()

    if not conn:
        return

    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO blaze_historico
                    (
                        rodada_id,
                        roll,
                        cor,
                        status,
                        coletado_em
                    )
                    VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
                    ON CONFLICT (rodada_id) DO NOTHING;
                """, (
                    round_id,
                    number,
                    color,
                    "complete"
                ))

    except Exception as e:
        add_log(
            f"⚠️ Erro salvando rodada {round_id}: {e}"
        )

    finally:
        conn.close()


def save_operation(
    signal_round_id,
    result_round_id,
    signal,
    result,
    number,
    votes_r,
    votes_b,
    confluence,
    profit
):
    conn = get_db()

    if not conn:
        return

    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO blaze_operacoes
                    (
                        rodada_sinal,
                        rodada_resultado,
                        sinal,
                        resultado,
                        numero,
                        votos_r,
                        votos_b,
                        confluencia,
                        valor,
                        lucro
                    )
                    VALUES
                    (
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s
                    );
                """, (
                    signal_round_id,
                    result_round_id,
                    signal,
                    result,
                    number,
                    votes_r,
                    votes_b,
                    confluence,
                    APOSTA_BASE,
                    profit
                ))

    except Exception as e:
        add_log(
            f"⚠️ Erro salvando operação: {e}"
        )

    finally:
        conn.close()


# ==============================================================================
# HISTÓRICO EM MEMÓRIA
# ==============================================================================

def add_history(round_id, number, color):
    with state_lock:
        history_rounds.append(str(round_id))
        history_numbers.append(int(number))
        history_colors.append(str(color).upper())

        processed_rounds.add(str(round_id))

        if len(history_numbers) > HISTORY_LIMIT:
            del history_rounds[:-HISTORY_LIMIT]
            del history_numbers[:-HISTORY_LIMIT]
            del history_colors[:-HISTORY_LIMIT]


# ==============================================================================
# V3 — REGRAS
# ==============================================================================

def calculate_v3():
    """
    V3 operacional.

    White continua dentro do histórico e pode influenciar a sequência,
    mas nunca é WIN nem LOSS.

    Regras:
      R1  SOMA2 <= 8             -> BLACK
      R2  SOMA3 <= 14            -> RED
      R3  SOMA3 >= 30            -> BLACK
      R4  DISTANCIA >= 9         -> RED
      R5  DISTANCIA >= 11        -> RED
      R6  NUMERO 3               -> RED
      R7  NUMERO 4               -> BLACK
      R8  NUMERO 6               -> BLACK
      R9  H10-H10-L7             -> BLACK
      R10 H7-H7-L1               -> BLACK
    """

    with state_lock:
        numbers = list(history_numbers[-10:])

    if len(numbers) < 10:
        return None

    n1 = numbers[-1]
    n2 = numbers[-2]
    n3 = numbers[-3]

    soma2 = n1 + n2
    soma3 = n1 + n2 + n3
    distancia = abs(n1 - n2)

    rules = {
        "R1": "B" if soma2 <= 8 else None,
        "R2": "R" if soma3 <= 14 else None,
        "R3": "B" if soma3 >= 30 else None,
        "R4": "R" if distancia >= 9 else None,
        "R5": "R" if distancia >= 11 else None,
        "R6": "R" if n1 == 3 else None,
        "R7": "B" if n1 == 4 else None,
        "R8": "B" if n1 == 6 else None,
        "R9": (
            "B"
            if n3 >= 8 and n2 >= 8 and n1 == 7
            else None
        ),
        "R10": (
            "B"
            if n3 >= 7 and n2 >= 7 and n1 <= 1
            else None
        ),
    }

    votes_r = [
        rule
        for rule, vote in rules.items()
        if vote == "R"
    ]

    votes_b = [
        rule
        for rule, vote in rules.items()
        if vote == "B"
    ]

    if (
        len(votes_r) > len(votes_b)
        and len(votes_r) >= MIN_CONFLUENCIA
    ):
        signal = "R"
        confluence = len(votes_r)

    elif (
        len(votes_b) > len(votes_r)
        and len(votes_b) >= MIN_CONFLUENCIA
    ):
        signal = "B"
        confluence = len(votes_b)

    else:
        signal = None
        confluence = max(
            len(votes_r),
            len(votes_b)
        )

    return {
        "signal": signal,
        "confluence": confluence,
        "votes_r": votes_r,
        "votes_b": votes_b,
        "rules": rules,
        "soma2": soma2,
        "soma3": soma3,
        "distancia": distancia,
        "n1": n1,
        "n2": n2,
        "n3": n3,
    }


# ==============================================================================
# CONTROLE DO BOT
# ==============================================================================

def start_bot():
    global bot_running
    global bot_state
    global signal_color
    global signal_votes
    global signal_reason
    global signal_round
    global wins
    global losses
    global current_profit
    global operations

    with state_lock:
        bot_running = True
        bot_state = "CAÇANDO"

        signal_color = None
        signal_votes = 0
        signal_reason = ""
        signal_round = None

        wins = 0
        losses = 0
        current_profit = 0.0
        operations = []

    add_log("")
    add_log("==========================================")
    add_log("🟢 BOT BLAZE V3 INICIADO")
    add_log("🎯 MÍNIMO DE CONFLUÊNCIA: 2")
    add_log("⚪ WHITE: IGNORADO NA AVALIAÇÃO")
    add_log(
        f"💰 APOSTA FIXA: R$ {APOSTA_BASE:.2f}"
    )
    add_log("==========================================")


def stop_bot():
    global bot_running
    global bot_state
    global signal_color
    global signal_votes
    global signal_reason
    global signal_round

    with state_lock:
        bot_running = False
        bot_state = "PARADO"

        signal_color = None
        signal_votes = 0
        signal_reason = ""
        signal_round = None

        saldo = current_profit

    add_log("")
    add_log("==========================================")
    add_log("🔴 BOT PARADO")
    add_log(
        f"💰 SALDO DA SESSÃO: R$ {saldo:.2f}"
    )
    add_log("==========================================")


def reset_session():
    global wins
    global losses
    global current_profit
    global operations

    with state_lock:
        wins = 0
        losses = 0
        current_profit = 0.0
        operations = []

    add_log("♻️ Sessão financeira zerada.")


# ==============================================================================
# PROCESSAMENTO DE UMA RODADA
# ==============================================================================

def process_result(round_id, number, color):
    global wins
    global losses
    global current_profit
    global bot_state
    global signal_color
    global signal_votes
    global signal_reason
    global signal_round

    round_id = str(round_id)
    color = str(color).upper()

    # --------------------------------------------------------------------------
    # 1. Se existe operação aberta, esta rodada resolve a operação.
    # --------------------------------------------------------------------------

    with state_lock:
        running = bot_running
        state = bot_state
        active_signal = signal_color
        active_votes = signal_votes
        active_reason = signal_reason
        active_signal_round = signal_round

    if (
        running
        and state == "ACOMPANHANDO"
        and active_signal
    ):

        # WHITE NÃO FECHA A OPERAÇÃO.
        if color == "W":
            save_result(
                round_id,
                number,
                color
            )

            add_history(
                round_id,
                number,
                color
            )

            add_log(
                f"⚪ WHITE — {number} — "
                f"rodada {round_id} ignorada. "
                f"Sinal {active_signal} continua aberto."
            )

            return

        result = (
            "WIN"
            if color == active_signal
            else "LOSS"
        )

        profit = (
            APOSTA_BASE
            if result == "WIN"
            else -APOSTA_BASE
        )

        with state_lock:

            if result == "WIN":
                wins += 1
            else:
                losses += 1

            current_profit += profit

            operations.append({
                "round": round_id,
                "result": result,
                "signal": active_signal,
                "number": number,
                "profit": profit,
                "votes": active_votes,
                "reason": active_reason
            })

            if len(operations) > 30:
                del operations[:-30]

            bot_state = "CAÇANDO"

            signal_color = None
            signal_votes = 0
            signal_reason = ""
            signal_round = None

        if result == "WIN":
            add_log(
                f"✅ WIN — SINAL {active_signal} | "
                f"Saiu {color} ({number}) | "
                f"+R$ {profit:.2f}"
            )
        else:
            add_log(
                f"❌ LOSS — SINAL {active_signal} | "
                f"Saiu {color} ({number}) | "
                f"-R$ {abs(profit):.2f}"
            )

        save_operation(
            active_signal_round,
            round_id,
            active_signal,
            result,
            number,
            0,
            0,
            active_votes,
            profit
        )

        save_result(
            round_id,
            number,
            color
        )

        add_history(
            round_id,
            number,
            color
        )

        return

    # --------------------------------------------------------------------------
    # 2. Primeiro salva/adiciona o resultado.
    # 3. Depois calcula o próximo sinal.
    #
    # Isso garante que o resultado recém-chegado NÃO influencia o próprio
    # sinal que deveria ter sido disparado antes dele.
    # --------------------------------------------------------------------------

    save_result(
        round_id,
        number,
        color
    )

    add_history(
        round_id,
        number,
        color
    )

    with state_lock:
        running = bot_running
        state = bot_state

    if not running or state != "CAÇANDO":
        return

    analysis = calculate_v3()

    if not analysis:
        add_log(
            "⏳ Aguardando 10 resultados para V3."
        )
        return

    votes_r = len(
        analysis["votes_r"]
    )

    votes_b = len(
        analysis["votes_b"]
    )

    if analysis["signal"]:

        with state_lock:
            bot_state = "ACOMPANHANDO"

            signal_color = analysis["signal"]

            signal_votes = analysis["confluence"]

            signal_reason = (
                f"{votes_r}R x {votes_b}B"
            )

            signal_round = round_id

        rules_used = (
            analysis["votes_r"]
            + analysis["votes_b"]
        )

        add_log(
            f"🚨 SINAL {analysis['signal']} "
            f"— {analysis['confluence']} VOTOS"
        )

        add_log(
            f"📊 Votos: R={votes_r} | "
            f"B={votes_b}"
        )

        add_log(
            f"🧠 Regras: "
            f"{', '.join(rules_used)}"
        )

        add_log(
            f"📐 SOMA2={analysis['soma2']} | "
            f"SOMA3={analysis['soma3']} | "
            f"DIST={analysis['distancia']}"
        )

        add_log(
            f"🚀 ENTRAR NO {analysis['signal']} "
            f"— R$ {APOSTA_BASE:.2f}"
        )

    else:

        if votes_r or votes_b:
            add_log(
                f"⚪ SEM ENTRADA — "
                f"R={votes_r} | B={votes_b}"
            )


# ==============================================================================
# SOCKET.IO — BLAZE
# ==============================================================================

def ack_is_ok(ack):
    if ack == "ok":
        return True

    if isinstance(ack, (list, tuple)):
        return (
            len(ack) > 0
            and ack[0] == "ok"
        )

    return False


def subscribe_room():
    try:
        sio.emit(
            "cmd",
            {
                "id": "subscribe",
                "payload": {
                    "room": ROOM
                }
            },
            callback=on_subscribe_ack
        )

    except Exception as e:
        add_log(
            f"⚠️ Erro enviando subscribe: {e}"
        )


def on_auth_ack(ack):
    if ack_is_ok(ack):
        add_log("🔐 Autenticação Blaze: OK.")
        add_log(
            f"📡 Inscrevendo na sala {ROOM}..."
        )

        subscribe_room()

    else:
        add_log(
            f"❌ Falha na autenticação Blaze: {ack}"
        )


def on_subscribe_ack(ack):
    if ack_is_ok(ack):
        add_log(
            f"✅ Sala {ROOM} inscrita."
        )

    else:
        add_log(
            f"❌ Falha ao assinar {ROOM}: {ack}"
        )


@sio.event
def connect():
    add_log("🔌 Socket.IO conectado.")

    if not BLAZE_TOKEN:
        add_log(
            "⚠️ BLAZE_TOKEN não configurado. "
            "O socket conectou, mas a assinatura pode falhar."
        )

        subscribe_room()
        return

    try:
        sio.emit(
            "cmd",
            {
                "id": "authenticate",
                "payload": {
                    "token": BLAZE_TOKEN
                }
            },
            callback=on_auth_ack
        )

        add_log("🔐 Enviando autenticação...")

    except Exception as e:
        add_log(
            f"⚠️ Erro enviando autenticação: {e}"
        )


@sio.event
def disconnect():
    add_log(
        "🔌 Socket.IO desconectado. "
        "Tentando reconectar..."
    )


@sio.event
def connect_error(data):
    add_log(
        f"⚠️ Erro de conexão Socket.IO: {data}"
    )


def parse_double_tick(message):
    """
    Formato esperado:
        {
            "id": "double.tick",
            "payload": {
                "status": "finished"/"complete",
                "color": 0/1/2,
                "roll": 0..14,
                ...
            }
        }
    """

    if not isinstance(message, dict):
        return None

    if message.get("id") != "double.tick":
        return None

    payload = message.get("payload")

    if not isinstance(payload, dict):
        return None

    status = str(
        payload.get("status", "")
    ).lower()

    if status not in (
        "finished",
        "complete"
    ):
        return None

    roll = payload.get("roll")

    try:
        roll = int(roll)
    except Exception:
        return None

    if not 0 <= roll <= 14:
        return None

    numeric_color = payload.get("color")

    if numeric_color == 0:
        color = "W"
    elif numeric_color == 1:
        color = "R"
    elif numeric_color == 2:
        color = "B"
    else:
        # Fallback pelo roll.
        if roll == 0:
            color = "W"
        elif 1 <= roll <= 7:
            color = "R"
        else:
            color = "B"

    round_id = (
        payload.get("id")
        or payload.get("round_id")
        or payload.get("roundId")
        or payload.get("issue")
        or payload.get("issueNumber")
        or payload.get("created_at")
    )

    if round_id is None:
        # created_at é usado apenas como último identificador
        # para não perder uma rodada.
        round_id = (
            payload.get("created_at")
        )

    if round_id is None:
        return None

    return (
        str(round_id),
        roll,
        color
    )


@sio.on("data")
def on_data(message):
    try:
        result = parse_double_tick(
            message
        )

        if not result:
            return

        round_id, number, color = result

        with state_lock:
            if round_id in processed_rounds:
                return

            # Reserva imediatamente para impedir duplicação.
            processed_rounds.add(round_id)

        process_result(
            round_id,
            number,
            color
        )

    except Exception as e:
        add_log(
            f"⚠️ Erro processando double.tick: {e}"
        )


def socket_loop():
    while True:

        try:

            if not sio.connected:

                add_log(
                    "📡 Conectando ao Blaze..."
                )

                sio.connect(
                    BLAZE_SOCKET_URL,
                    transports=["websocket"],
                    socketio_path=SOCKETIO_PATH,
                    wait_timeout=15
                )

            sio.sleep(20)

        except Exception as e:

            add_log(
                f"⚠️ Socket: {e}"
            )

            try:
                if sio.connected:
                    sio.disconnect()
            except Exception:
                pass

            time.sleep(5)


# ==============================================================================
# FLASK
# ==============================================================================

HTML = r"""
<!DOCTYPE html>
<html lang="pt-br">

<head>

<meta charset="UTF-8">

<meta name="viewport"
      content="width=device-width, initial-scale=1.0">

<meta http-equiv="refresh" content="8">

<title>Blaze Double V3</title>

<style>

:root {
    --bg: #07090d;
    --card: #10151c;
    --card2: #0d1218;
    --line: rgba(255,255,255,0.08);

    --text: #edf2f7;
    --muted: #7e8996;

    --red: #ff4655;
    --black: #cbd2dc;
    --white: #ffffff;

    --green: #19df83;
    --yellow: #ffc857;
    --blue: #5ca9ff;
}

* {
    box-sizing: border-box;
    margin: 0;
    padding: 0;
}

body {
    min-height: 100vh;

    background:
        radial-gradient(
            circle at top,
            #18202b 0%,
            var(--bg) 55%
        );

    color: var(--text);

    font-family:
        Inter,
        Arial,
        sans-serif;

    padding: 18px;
}

.container {
    max-width: 1180px;
    margin: auto;
}

.topbar {
    display: flex;
    align-items: center;
    justify-content: space-between;

    gap: 15px;
    flex-wrap: wrap;

    margin-bottom: 16px;
}

.brand {
    font-size: 25px;
    font-weight: 900;
}

.subtitle {
    color: var(--muted);
    font-size: 12px;
    margin-top: 5px;
}

.controls {
    display: flex;
    gap: 8px;
    align-items: center;
    flex-wrap: wrap;
}

.badge {
    padding: 8px 13px;
    border-radius: 99px;

    border: 1px solid var(--line);

    font-size: 12px;
    font-weight: 900;
}

.badge-on {
    color: var(--green);
    border-color: rgba(25,223,131,.5);
    background: rgba(25,223,131,.08);
}

.badge-off {
    color: var(--red);
    border-color: rgba(255,70,85,.5);
    background: rgba(255,70,85,.08);
}

button {
    border: 0;
    border-radius: 10px;

    padding: 10px 14px;

    font-weight: 900;
    cursor: pointer;
}

.btn-start {
    background: var(--green);
    color: #00180c;
}

.btn-stop {
    background: var(--red);
    color: white;
}

.btn-reset {
    background: #252d37;
    color: white;
}

.signal-card {
    background:
        linear-gradient(
            135deg,
            rgba(255,255,255,.035),
            rgba(255,255,255,.01)
        );

    border: 1px solid var(--line);
    border-radius: 16px;

    padding: 17px;

    margin-bottom: 14px;
}

.signal-main {
    font-size: 23px;
    font-weight: 950;
}

.signal-red {
    color: var(--red);
}

.signal-black {
    color: var(--black);
}

.hunting {
    color: var(--yellow);
}

.signal-info {
    color: var(--muted);
    font-size: 12px;
    margin-top: 7px;
}

.stats {
    display: grid;

    grid-template-columns:
        repeat(6, 1fr);

    gap: 10px;

    margin-bottom: 14px;
}

.card {
    background: rgba(16,21,28,.82);

    border: 1px solid var(--line);
    border-radius: 14px;

    padding: 14px;
}

.card-label {
    color: var(--muted);

    font-size: 10px;
    font-weight: 900;

    text-transform: uppercase;
}

.card-value {
    font-size: 24px;
    font-weight: 950;

    margin-top: 6px;
}

.positive {
    color: var(--green);
}

.negative {
    color: var(--red);
}

.trend {
    display: flex;

    gap: 6px;

    overflow-x: auto;

    padding: 10px;

    margin-bottom: 14px;

    background: var(--card2);

    border:
        1px solid
        var(--line);

    border-radius: 14px;
}

.pill {
    min-width: 38px;
    height: 38px;

    border-radius: 9px;

    display: flex;
    align-items: center;
    justify-content: center;

    font-size: 13px;
    font-weight: 950;
}

.pill-r {
    background: var(--red);
    color: white;
}

.pill-b {
    background: #252b34;
    color: white;

    border:
        1px solid
        #65707d;
}

.pill-w {
    background: white;
    color: #111;
}

.content {
    display: grid;

    grid-template-columns:
        1.45fr
        .85fr;

    gap: 14px;
}

.panel {
    background: rgba(16,21,28,.82);

    border:
        1px solid
        var(--line);

    border-radius: 14px;

    padding: 16px;
}

.panel-title {
    color: var(--muted);

    font-size: 11px;
    font-weight: 900;

    text-transform: uppercase;

    margin-bottom: 12px;
}

.console {
    height: 390px;

    overflow-y: auto;

    font-family:
        ui-monospace,
        Consolas,
        monospace;

    font-size: 12px;
}

.log-line {
    padding: 6px 0;

    border-bottom:
        1px solid
        rgba(255,255,255,.035);
}

.operations {
    height: 390px;

    overflow-y: auto;
}

.operation {
    display: grid;

    grid-template-columns:
        1fr
        auto
        auto;

    gap: 8px;

    align-items: center;

    padding: 9px 0;

    border-bottom:
        1px solid
        rgba(255,255,255,.035);

    font-size: 12px;
}

.win {
    color: var(--green);
    font-weight: 900;
}

.loss {
    color: var(--red);
    font-weight: 900;
}

.muted {
    color: var(--muted);
}

.footer {
    color: var(--muted);

    text-align: center;

    font-size: 10px;

    margin-top: 15px;
}

@media (max-width: 950px) {

    .stats {
        grid-template-columns:
            repeat(3, 1fr);
    }

}

@media (max-width: 650px) {

    body {
        padding: 10px;
    }

    .stats {
        grid-template-columns:
            repeat(2, 1fr);
    }

    .content {
        grid-template-columns: 1fr;
    }

    .console,
    .operations {
        height: 280px;
    }

    .controls {
        justify-content: center;
        width: 100%;
    }

}

</style>

</head>

<body>

<div class="container">

    <div class="topbar">

        <div>

            <div class="brand">
                🤖 Blaze Double V3
            </div>

            <div class="subtitle">
                Confluência R/B • White fora da avaliação •
                mínimo {{ min_conf }} votos
            </div>

        </div>

        <div class="controls">

            <div class="
                badge
                {{ 'badge-on' if running else 'badge-off' }}
            ">

                {{ '● OPERANDO' if running else '● PARADO' }}

            </div>

            <form method="POST"
                  action="/start">

                <button class="btn-start">
                    ▶ INICIAR
                </button>

            </form>

            <form method="POST"
                  action="/stop">

                <button class="btn-stop">
                    ⏹ PARAR
                </button>

            </form>

            <form method="POST"
                  action="/reset">

                <button class="btn-reset">
                    ↻ ZERAR
                </button>

            </form>

        </div>

    </div>


    <div class="signal-card">

        {% if running and state == 'ACOMPANHANDO' %}

            <div class="
                signal-main
                {{ 'signal-red'
                   if signal == 'R'
                   else 'signal-black' }}
            ">

                🚨 ENTRAR {{ signal }}
                — {{ votes }} VOTOS

            </div>

            <div class="signal-info">

                Confluência:
                {{ reason }}

                • Aposta:
                R$ {{ '%.2f'|format(base) }}

                • White não encerra o sinal.

            </div>

        {% elif running %}

            <div class="signal-main hunting">

                🎯 CAÇANDO SINAL V3

            </div>

            <div class="signal-info">

                Aguardando confluência mínima de
                {{ min_conf }} votos.

            </div>

        {% else %}

            <div class="signal-main muted">

                ⏹ BOT PARADO

            </div>

            <div class="signal-info">

                O histórico continua sendo coletado
                pelo Socket.IO.

            </div>

        {% endif %}

    </div>


    <div class="stats">

        <div class="card">

            <div class="card-label">
                Saldo sessão
            </div>

            <div class="
                card-value
                {{ 'positive'
                   if profit >= 0
                   else 'negative' }}
            ">

                R$ {{ '%.2f'|format(profit) }}

            </div>

        </div>


        <div class="card">

            <div class="card-label">
                Vitórias
            </div>

            <div class="card-value positive">
                {{ wins }}
            </div>

        </div>


        <div class="card">

            <div class="card-label">
                Derrotas
            </div>

            <div class="card-value negative">
                {{ losses }}
            </div>

        </div>


        <div class="card">

            <div class="card-label">
                Win rate
            </div>

            <div class="card-value">
                {{ win_rate }}%
            </div>

        </div>


        <div class="card">

            <div class="card-label">
                Operações
            </div>

            <div class="card-value">
                {{ signals }}
            </div>

        </div>


        <div class="card">

            <div class="card-label">
                Histórico
            </div>

            <div class="card-value">
                {{ hist_len }}
            </div>

        </div>

    </div>


    <div class="trend">

        {% for i in range(history_numbers|length) %}

            <div class="
                pill
                pill-{{ history_colors[i]|lower }}
            ">

                {{ history_numbers[i] }}

            </div>

        {% endfor %}

    </div>


    <div class="content">


        <div class="panel">

            <div class="panel-title">
                Console V3
            </div>

            <div class="console"
                 id="console">

                {% for line in logs %}

                    <div class="log-line">
                        {{ line }}
                    </div>

                {% endfor %}

            </div>

        </div>


        <div class="panel">

            <div class="panel-title">
                Últimas operações
            </div>

            <div class="operations">

                {% if not operations %}

                    <div class="muted">
                        Nenhuma operação nesta sessão.
                    </div>

                {% endif %}


                {% for op in operations|reverse %}

                    <div class="operation">

                        <span>
                            {{ op.round }}
                        </span>

                        <span class="
                            {{ 'win'
                               if op.result == 'WIN'
                               else 'loss' }}
                        ">

                            {{ op.result }}

                        </span>

                        <span class="
                            {{ 'win'
                               if op.result == 'WIN'
                               else 'loss' }}
                        ">

                            {{ '%+.2f'|format(op.profit) }}

                        </span>

                    </div>

                {% endfor %}

            </div>

        </div>

    </div>


    <div class="footer">

        Blaze Double V3 •
        R{{ min_conf }} de confluência •
        RED / BLACK operacionais •
        WHITE ignorado na avaliação

    </div>

</div>


<script>

const consoleBox =
    document.getElementById("console");

if (consoleBox) {
    consoleBox.scrollTop =
        consoleBox.scrollHeight;
}

</script>

</body>

</html>
"""


# ==============================================================================
# MÉTRICAS
# ==============================================================================

def get_win_rate():
    with state_lock:
        total = wins + losses

        if total == 0:
            return 0.0

        return round(
            (wins / total) * 100,
            1
        )


# ==============================================================================
# ROTAS
# ==============================================================================

@app.route("/")
def home():

    with state_lock:

        context = {
            "logs": list(logs),

            "wins": wins,
            "losses": losses,

            "profit": current_profit,

            "running": bot_running,
            "state": bot_state,

            "signal": signal_color,
            "votes": signal_votes,
            "reason": signal_reason,

            "operations": list(operations),

            "history_numbers":
                list(history_numbers),

            "history_colors":
                list(history_colors),

            "hist_len":
                len(history_numbers)
        }

    context["win_rate"] = get_win_rate()

    context["signals"] = (
        context["wins"]
        + context["losses"]
    )

    context["base"] = APOSTA_BASE
    context["min_conf"] = MIN_CONFLUENCIA

    return render_template_string(
        HTML,
        **context
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
    reset_session()
    return redirect("/")


@app.route("/health")
def health():
    return {
        "status": "ok",
        "bot_running": bot_running,
        "socket_connected": sio.connected
    }, 200


# ==============================================================================
# START
# ==============================================================================

def boot():

    init_db()

    load_history()

    add_log(
        "🤖 Blaze Double V3 carregado."
    )

    add_log(
        f"⚙️ MIN_CONFLUENCIA={MIN_CONFLUENCIA}"
    )

    add_log(
        f"💰 APOSTA_BASE=R$ {APOSTA_BASE:.2f}"
    )

    if BLAZE_TOKEN:
        add_log(
            "🔐 BLAZE_TOKEN encontrado nas variáveis de ambiente."
        )
    else:
        add_log(
            "⚠️ BLAZE_TOKEN não encontrado."
        )

    thread = threading.Thread(
        target=socket_loop,
        daemon=True
    )

    thread.start()


if __name__ == "__main__":

    boot()

    port = int(
        os.getenv(
            "PORT",
            "5000"
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
        threaded=True
    )
