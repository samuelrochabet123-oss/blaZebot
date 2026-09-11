# ================================================================
# BLAZE BOT — WEB APP PARA RENDER + NEON
# ================================================================
# O collector roda em uma thread dentro do mesmo Web Service.
#
# IMPORTANTE NO RENDER:
#   Start Command:
#   gunicorn --workers 1 --bind 0.0.0.0:$PORT app:app
# ================================================================

import os
import threading
from datetime import datetime

import psycopg2
from flask import Flask, jsonify, render_template_string


app = Flask(__name__)


# ================================================================
# CONFIGURAÇÃO
# ================================================================

APOSTA_BASE = 1.00

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


# ================================================================
# BANCO
# ================================================================

def get_db_connection():
    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        return None

    try:
        if "sslmode=" not in database_url:
            separator = "&" if "?" in database_url else "?"
            database_url = f"{database_url}{separator}sslmode=require"

        return psycopg2.connect(
            database_url,
            connect_timeout=10,
        )

    except Exception as e:
        print(f"Erro Neon: {e}")
        return None


def init_web_db():
    conn = get_db_connection()

    if not conn:
        return

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

    except Exception as e:
        print(f"Erro init web DB: {e}")

        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass


def consultar_dashboard():
    conn = get_db_connection()

    vazio = {
        "conectado": False,
        "ultima_rodada": None,
        "ultimo_resultado_em": None,
        "total_ticks": 0,
        "total_resultados": 0,
        "total_duplicados": 0,
        "total_erros_db": 0,
        "total_jogos": 0,
        "jogos": [],
    }

    if not conn:
        return vazio

    try:
        with conn.cursor() as cur:

            cur.execute("""
                SELECT
                    conectado,
                    ultima_rodada,
                    ultimo_resultado_em,
                    total_ticks,
                    total_resultados,
                    total_duplicados,
                    total_erros_db
                FROM collector_status
                WHERE id = 1;
            """)

            row = cur.fetchone()

            if row:
                (
                    vazio["conectado"],
                    vazio["ultima_rodada"],
                    vazio["ultimo_resultado_em"],
                    vazio["total_ticks"],
                    vazio["total_resultados"],
                    vazio["total_duplicados"],
                    vazio["total_erros_db"],
                ) = row

            cur.execute("SELECT COUNT(*) FROM blaze_historico;")
            vazio["total_jogos"] = cur.fetchone()[0]

            cur.execute("""
                SELECT roll, color, cor, rodada_id
                FROM blaze_historico
                ORDER BY id DESC
                LIMIT 20;
            """)

            rows = cur.fetchall()

            vazio["jogos"] = list(reversed(rows))

        conn.close()
        return vazio

    except Exception as e:
        print(f"Erro dashboard: {e}")

        try:
            conn.close()
        except Exception:
            pass

        return vazio


# ================================================================
# HTML
# ================================================================

HTML = r"""
<!DOCTYPE html>
<html lang="pt-br">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="10">

<title>Blaze Collector — Render + Neon</title>

<style>
:root {
    --bg: #07090d;
    --card: #11151c;
    --border: rgba(255,255,255,.08);
    --text: #e8edf3;
    --muted: #7d8794;
    --red: #ff4d57;
    --green: #19df78;
    --white: #f1f3f5;
    --yellow: #ffc247;
}

* {
    box-sizing: border-box;
    margin: 0;
    padding: 0;
}

body {
    background: radial-gradient(circle at top, #171d28 0%, var(--bg) 52%);
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
    margin-bottom: 15px;
}

.logo {
    font-size: 25px;
    font-weight: 900;
}

.logo span {
    color: var(--red);
}

.status-grid {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 10px;
    margin-bottom: 12px;
}

.status-card,
.card,
.signal-box,
.trend {
    background: rgba(17,21,28,.86);
    border: 1px solid var(--border);
    border-radius: 14px;
}

.status-card,
.card {
    padding: 15px;
}

.status-title {
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

.stats {
    display: grid;
    grid-template-columns: repeat(5, 1fr);
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

.signal-box {
    margin-bottom: 12px;
    padding: 20px;
    text-align: center;
}

.signal-title {
    font-size: 25px;
    font-weight: 900;
}

.signal-detail {
    margin-top: 8px;
    color: var(--muted);
    font-size: 13px;
}

.trend-section {
    margin-top: 15px;
}

.section-title {
    color: var(--muted);
    font-size: 11px;
    font-weight: 900;
    text-transform: uppercase;
    margin-bottom: 10px;
}

.trend {
    display: flex;
    gap: 6px;
    overflow-x: auto;
    padding: 10px;
}

.pill {
    min-width: 42px;
    height: 42px;
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

@media (max-width: 800px) {
    .status-grid,
    .stats {
        grid-template-columns: repeat(2, 1fr);
    }
}
</style>
</head>

<body>

<div class="container">

    <div class="header">
        <div class="logo">
            🤖 Blaze <span>Collector</span>
        </div>
    </div>

    <div class="status-grid">

        <div class="status-card">
            <div class="status-title">Socket</div>
            {% if status.conectado %}
                <div class="status-value online">🟢 CONECTADO</div>
            {% else %}
                <div class="status-value offline">🔴 DESCONECTADO</div>
            {% endif %}
        </div>

        <div class="status-card">
            <div class="status-title">Última rodada</div>
            <div class="status-value">
                {{ status.ultima_rodada or 'Aguardando...' }}
            </div>
        </div>

        <div class="status-card">
            <div class="status-title">Último resultado</div>
            <div class="status-value">
                {{ status.ultimo_resultado_em or 'Aguardando...' }}
            </div>
        </div>

        <div class="status-card">
            <div class="status-title">Jogos no Neon</div>
            <div class="value yellow">
                {{ status.total_jogos }}
            </div>
        </div>

    </div>

    <div class="signal-box">

        {% if status.conectado %}
            <div class="signal-title online">
                🟢 COLETANDO EM TEMPO REAL
            </div>
            <div class="signal-detail">
                Socket.IO → double.tick → Neon PostgreSQL
            </div>
        {% else %}
            <div class="signal-title red">
                🔴 AGUARDANDO CONEXÃO
            </div>
            <div class="signal-detail">
                O coletor continuará tentando reconectar automaticamente.
            </div>
        {% endif %}

    </div>

    <div class="stats">

        <div class="card">
            <div class="status-title">Resultados novos</div>
            <div class="value green">
                {{ status.total_resultados }}
            </div>
        </div>

        <div class="card">
            <div class="status-title">Ticks</div>
            <div class="value">
                {{ status.total_ticks }}
            </div>
        </div>

        <div class="card">
            <div class="status-title">Duplicados</div>
            <div class="value">
                {{ status.total_duplicados }}
            </div>
        </div>

        <div class="card">
            <div class="status-title">Erros DB</div>
            <div class="value red">
                {{ status.total_erros_db }}
            </div>
        </div>

        <div class="card">
            <div class="status-title">Banco</div>
            <div class="value green">
                NEON
            </div>
        </div>

    </div>

    <div class="trend-section">

        <div class="section-title">
            Últimos jogos coletados
        </div>

        <div class="trend">

            {% for jogo in status.jogos %}

                {% set roll = jogo[0] %}
                {% set color = jogo[1] %}

                {% if color == 0 %}
                    <div class="pill pill-w">{{ roll }}</div>
                {% elif color == 1 %}
                    <div class="pill pill-r">{{ roll }}</div>
                {% else %}
                    <div class="pill pill-b">{{ roll }}</div>
                {% endif %}

            {% endfor %}

        </div>

    </div>

</div>

</body>
</html>
"""


# ================================================================
# ROTAS
# ================================================================

@app.route("/")
def home():
    status = consultar_dashboard()

    return render_template_string(
        HTML,
        status=status,
    )


@app.route("/health")
def health():
    return jsonify({
        "status": "ok"
    }), 200


@app.route("/status")
def status():
    return jsonify(consultar_dashboard()), 200


# ================================================================
# INICIAR COLLECTOR
# ================================================================

def iniciar_background_collector():
    try:
        from collector import iniciar_coletor_em_thread

        print("🚀 Iniciando Collector V2.2 em background...")
        iniciar_coletor_em_thread()

    except Exception as e:
        print(f"❌ Não foi possível iniciar o collector: {e}")


# Inicializa banco e coletor uma vez por worker.
init_web_db()

if os.getenv("RUN_COLLECTOR", "true").lower() == "true":
    collector_thread = threading.Thread(
        target=iniciar_background_collector,
        name="collector-bootstrap",
        daemon=True,
    )
    collector_thread.start()
