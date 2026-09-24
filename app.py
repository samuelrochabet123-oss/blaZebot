# ================================================================
# BLAZE BOT — DASHBOARD WEB + COLLECTOR + MOTOR ESTATÍSTICO
#
# VERSÃO: V8.0 — GOOGLE PLANILHAS EDITION
#
# BANCO DE DADOS: Google Planilhas (sheets_db.py)
# Variáveis de ambiente:
#   GOOGLE_SHEETS_ID         -> ID da planilha (URL)
#   GOOGLE_CREDENTIALS_JSON  -> conteúdo do JSON da conta de serviço
# ================================================================


import os
import threading
import time

from flask import Flask, jsonify, render_template_string, redirect

import sheets_db as db


app = Flask(__name__)


# ================================================================
# INICIALIZAÇÃO DO "BANCO" (GOOGLE PLANILHAS)
# ================================================================

def init_web_db():

    if db.init_db():
        print("✅ Planilha inicializada.", flush=True)
    else:
        print(
            "❌ Falha ao inicializar a planilha. "
            "Verifique GOOGLE_SHEETS_ID e as credenciais.",
            flush=True
        )


# ================================================================
# INFORMAÇÕES DAS CORES PARA O DASHBOARD
# ================================================================

def cor_info(color, cor_texto=None):
    try:
        color = int(color)
    except Exception:
        color = None

    if color == 0:
        return {"sigla": "W", "nome": "BRANCO", "classe": "white", "emoji": "⚪"}
    if color == 1:
        return {"sigla": "R", "nome": "VERMELHO", "classe": "red", "emoji": "🔴"}
    if color == 2:
        return {"sigla": "P", "nome": "PRETO", "classe": "black", "emoji": "⚫"}

    texto = (cor_texto or "").upper().strip()
    if "VERMELHO" in texto or texto in {"RED", "R", "V", "VI"}:
        return {"sigla": "R", "nome": "VERMELHO", "classe": "red", "emoji": "🔴"}
    if "PRETO" in texto or texto in {"BLACK", "P", "B"}:
        return {"sigla": "P", "nome": "PRETO", "classe": "black", "emoji": "⚫"}
    return {"sigla": "W", "nome": "BRANCO", "classe": "white", "emoji": "⚪"}


def estrategia_curta(nome):
  if not nome:
    return "—"

  if "Z-Score Unificado" in nome:
    return "Z-Score Consolidado"
  return nome


# ================================================================
# DASHBOARD — LEITURA DA PLANILHA (COM CACHE)
# ================================================================

_DASH_CACHE = {"t": 0.0, "dados": None}
_DASH_TTL = 6  # protege a cota da API (o front atualiza a cada 5s)


def consultar_dashboard():
    agora_ts = time.time()
    if _DASH_CACHE["dados"] is not None and agora_ts - _DASH_CACHE["t"] < _DASH_TTL:
        return _DASH_CACHE["dados"]

    vazio = {
        "conectado": False,
        "ultima_rodada": None,
        "ultimo_resultado_em": None,
        "total_jogos": 0,
        "jogos": [],
        "motor": {
            "ativo": False, "sinal": False, "cor": None, "estrategia": None,
            "wins": 0, "losses": 0, "pendentes": 0, "profit": 0.0,
            "ciclo_ativo": False, "tentativa": 0,
            "cor_regra": None, "cor_entrada": None,
        },
        "historico_sinais": [],
    }

    try:
        # ---- STATUS DO COLLECTOR ----
        collector = db.ler_collector()
        vazio["conectado"] = db.to_bool(collector.get("conectado"))
        vazio["ultima_rodada"] = db.txt(collector.get("ultima_rodada")) or None
        vazio["ultimo_resultado_em"] = db.txt(collector.get("ultimo_resultado_em")) or None

        # ---- ESTADO DO MOTOR ----
        estado = db.ler_estado()
        inicio_sessao = db.txt(estado.get("inicio_sessao")) or None
        motor_ativo = db.to_bool(estado.get("motor_ativo"))
        ciclo_ativo = db.to_bool(estado.get("ciclo_ativo"))
        tentativa_atual = db.to_int(estado.get("tentativa_atual"), 0)
        ciclo_cor_regra = db.txt(estado.get("ciclo_cor_regra")) or None
        ciclo_cor_entrada = db.txt(estado.get("ciclo_cor_entrada")) or None
        ciclo_estrategia = db.txt(estado.get("ciclo_estrategia")) or None

        # ---- PLACAR DA SESSÃO ----
        # WHITE já é LOSS em estrategia_sinais. Portanto só existem
        # WIN, LOSS e PENDENTE no placar operacional.
        if inicio_sessao:
            sinais = [s for s in db.sinais_todos()
                      if s["criado_em"] and s["criado_em"] >= inicio_sessao]
        else:
            sinais = []

        wins = sum(1 for s in sinais if s["resultado"] == "WIN")
        losses = sum(1 for s in sinais if s["resultado"] == "LOSS")
        pendentes = sum(1 for s in sinais if s["resultado"] == "PENDENTE")
        profit = (wins - losses) * 1.0

        # ---- SINAL ATIVO — SOMENTE DA SESSÃO ATUAL ----
        sinal = None
        if inicio_sessao and motor_ativo and ciclo_ativo:
            pend = [s for s in sinais if s["resultado"] == "PENDENTE"]
            sinal = pend[-1] if pend else None

        if sinal:
            vazio["motor"] = {
                "ativo": motor_ativo, "sinal": True,
                "cor": sinal["cor_prevista"],
                "cor_regra": sinal["cor_regra"] or ciclo_cor_regra,
                "cor_entrada": sinal["cor_entrada"] or ciclo_cor_entrada,
                "tentativa": int(sinal["tentativa"] or tentativa_atual),
                "valor_aposta": float(sinal["valor_aposta"] or 1.0),
                "ciclo_ativo": True,
                "estrategia": estrategia_curta(sinal["estrategia"]),
                "wins": wins, "losses": losses, "pendentes": pendentes,
                "profit": profit,
            }
        else:
            vazio["motor"] = {
                "ativo": motor_ativo, "sinal": False,
                "cor": ciclo_cor_entrada if ciclo_ativo else None,
                "cor_regra": ciclo_cor_regra if ciclo_ativo else None,
                "cor_entrada": ciclo_cor_entrada if ciclo_ativo else None,
                "tentativa": tentativa_atual if ciclo_ativo else 0,
                "valor_aposta": 1.0,
                "ciclo_ativo": ciclo_ativo,
                "estrategia": estrategia_curta(ciclo_estrategia) if ciclo_ativo else None,
                "wins": wins, "losses": losses, "pendentes": pendentes,
                "profit": profit,
            }

        # ---- RODADAS DA SESSÃO ----
        if inicio_sessao:
            rows = db.rodadas_da_sessao(inicio_sessao, 24)
            rows = list(reversed(rows))
        else:
            rows = []

        vazio["total_jogos"] = len(rows)
        for r in rows:
            vazio["jogos"].append({
                "roll": r["roll"],
                "color": r["color"],
                "cor": r["cor"],
                "rodada": r["rodada_id"],
                **cor_info(r["color"], r["cor"]),
            })

        # ---- SINAIS DA SESSÃO — NUNCA DO HISTÓRICO ANTERIOR ----
        if inicio_sessao:
            for s in list(reversed(sinais))[:12]:
                resultado = s["resultado"]
                if resultado == "WHITE":
                    resultado = "LOSS"

                vazio["historico_sinais"].append({
                    "estrategia": estrategia_curta(s["estrategia"]),
                    "estrategia_full": s["estrategia"],
                    "prevista": s["cor_prevista"],
                    "base": s["rodada_base"],
                    "rodada_resultado": s["rodada_resultado"],
                    "cor_resultado": s["cor_resultado"],
                    "resultado": resultado,
                    "criado_em": s["criado_em"],
                    "tentativa": int(s["tentativa"] or 1),
                    "ciclo_id": s["ciclo_id"],
                    "cor_regra": s["cor_regra"],
                    "cor_entrada": s["cor_entrada"] or s["cor_prevista"],
                    "valor_aposta": float(s["valor_aposta"] or 1.0),
                })

        _DASH_CACHE["t"] = agora_ts
        _DASH_CACHE["dados"] = vazio
        return vazio

    except Exception as e:
        print(f"❌ Erro dashboard: {e}", flush=True)
        return vazio


HTML = r"""
<!DOCTYPE html>
<html lang="pt-br">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="5">
<title>Blaze Bot</title>
<style>
:root {
    --bg: #07090d;
    --panel: #10141b;
    --panel2: #151a22;
    --border: rgba(255,255,255,.08);
    --text: #eef2f7;
    --muted: #8993a1;
    --red: #ff4d5d;
    --green: #23e27c;
    --yellow: #ffc857;
    --white: #f4f5f7;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
    min-height: 100vh;
    background: radial-gradient(circle at top, #1a202b 0%, var(--bg) 48%);
    color: var(--text);
    font-family: Inter, Arial, sans-serif;
    padding: 20px;
}
.container { max-width: 1180px; margin: 0 auto; }
.header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 16px;
    gap: 15px;
    flex-wrap: wrap;
}
.logo { font-size: 25px; font-weight: 900; }
.logo span { color: var(--red); }
.header-right { display: flex; align-items: center; gap: 15px; }
.live {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 12px;
    font-weight: 900;
    color: var(--green);
}
.dot {
    width: 9px;
    height: 9px;
    border-radius: 50%;
    background: currentColor;
    box-shadow: 0 0 12px currentColor;
}
.top-grid {
    display: grid;
    grid-template-columns: 1.2fr 1fr 1fr 1fr;
    gap: 10px;
    margin-bottom: 12px;
}
.card {
    background: rgba(16,20,27,.92);
    border: 1px solid var(--border);
    border-radius: 15px;
    padding: 15px;
}
.label {
    color: var(--muted);
    font-size: 10px;
    font-weight: 900;
    text-transform: uppercase;
    letter-spacing: .7px;
    margin-bottom: 7px;
}
.big { font-size: 23px; font-weight: 950; }
.small { color: var(--muted); font-size: 11px; margin-top: 5px; }
.green { color: var(--green); }
.red { color: var(--red); }
.yellow { color: var(--yellow); }
.white { color: var(--white); }
.black { color: #d7dce3; }
.signal {
    margin-bottom: 12px;
    padding: 24px;
    border-radius: 18px;
    background: linear-gradient(145deg, rgba(22,27,36,.98), rgba(12,15,21,.98));
    border: 1px solid rgba(255,255,255,.10);
    text-align: center;
}
.signal.active {
    border-color: rgba(35,226,124,.28);
    box-shadow: 0 0 35px rgba(35,226,124,.06);
}
.signal .eyebrow {
    color: var(--muted);
    font-size: 11px;
    font-weight: 900;
    text-transform: uppercase;
    letter-spacing: 1px;
}
.signal .color { font-size: 38px; font-weight: 950; margin: 7px 0 4px; }
.signal .strategy { font-size: 15px; font-weight: 850; }
.signal .entry { margin-top: 9px; color: var(--muted); font-size: 12px; }
.section-title {
    display: flex;
    justify-content: space-between;
    align-items: end;
    margin: 18px 0 9px;
}
.section-title h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .8px; }
.section-title span { color: var(--muted); font-size: 11px; }
.performance {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 9px;
    margin-bottom: 12px;
}
.metric {
    text-align: center;
    padding: 13px 8px;
    background: rgba(16,20,27,.92);
    border: 1px solid var(--border);
    border-radius: 13px;
}
.metric .num { font-size: 22px; font-weight: 950; }
.metric .m-label {
    margin-top: 4px;
    color: var(--muted);
    font-size: 9px;
    text-transform: uppercase;
    font-weight: 900;
}
.history {
    background: rgba(16,20,27,.92);
    border: 1px solid var(--border);
    border-radius: 15px;
    overflow: hidden;
}
.history-row {
    display: grid;
    grid-template-columns: 1.7fr .7fr 1fr 1fr 1fr;
    gap: 8px;
    align-items: center;
    padding: 11px 13px;
    border-bottom: 1px solid rgba(255,255,255,.045);
    font-size: 11px;
}
.history-row:last-child { border-bottom: 0; }
.history-head {
    color: var(--muted);
    font-size: 9px;
    font-weight: 900;
    text-transform: uppercase;
}
.result-badge {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 58px;
    padding: 5px 7px;
    border-radius: 7px;
    font-size: 9px;
    font-weight: 950;
}
.result-win { background: rgba(35,226,124,.12); color: var(--green); }
.result-loss { background: rgba(255,77,93,.12); color: var(--red); }
.results {
    display: flex;
    gap: 7px;
    overflow-x: auto;
    padding: 12px;
    background: rgba(16,20,27,.92);
    border: 1px solid var(--border);
    border-radius: 15px;
}
.pill {
    flex: 0 0 auto;
    width: 48px;
    height: 48px;
    border-radius: 10px;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    font-weight: 950;
}
.pill small { font-size: 8px; opacity: .75; margin-top: 2px; }
.pill.red-bg { background: var(--red); color: white; }
.pill.black-bg { background: #262c35; color: white; border: 1px solid #444b55; }
.pill.white-bg { background: white; color: #111; }
.footer { text-align: center; color: #56606d; font-size: 9px; padding: 18px 0 4px; }
.control-btn {
    border: none;
    padding: 12px 24px;
    border-radius: 12px;
    font-weight: 900;
    cursor: pointer;
    font-size: 14px;
    color: white;
}
.control-btn.start { background: var(--green); }
.control-btn.pause { background: var(--red); }
.empty { padding: 25px; text-align: center; color: var(--muted); font-size: 12px; }
@media (max-width: 900px) {
    .top-grid { grid-template-columns: repeat(2,1fr); }
    .performance { grid-template-columns: repeat(2,1fr); }
}
@media (max-width: 600px) {
    body { padding: 12px; }
    .top-grid { grid-template-columns: 1fr; }
    .performance { grid-template-columns: repeat(2,1fr); }
    .history-row { grid-template-columns: 1.4fr .7fr 1fr 1fr; }
    .hide-mobile { display: none; }
}
</style>
</head>
<body>
<div class="container">

    <div class="header">
        <div class="logo">🤖 Blaze <span>Bot</span></div>
        <div class="header-right">
            <form action="/controle_motor" method="POST">
                {% if status.motor.ativo %}
                    <button type="submit" class="control-btn pause">⏸️ PAUSAR</button>
                {% else %}
                    <button type="submit" class="control-btn start">▶️ INICIAR</button>
                {% endif %}
            </form>
            <div class="live">
                <span class="dot"></span>
                {% if status.conectado %}COLETOR ONLINE{% else %}COLETOR OFFLINE{% endif %}
            </div>
        </div>
    </div>

    <div class="top-grid">
        <div class="card">
            <div class="label">Última rodada</div>
            <div class="big">{{ status.ultima_rodada or 'Aguardando...' }}</div>
            <div class="small">Atualização automática a cada 5s</div>
        </div>
        <div class="card">
            <div class="label">Rodadas da sessão</div>
            <div class="big yellow">{{ status.total_jogos }}</div>
            <div class="small">Desde o último INICIAR</div>
        </div>
        <div class="card">
            <div class="label">Motor</div>
            {% if status.motor.ativo %}
                <div class="big green">🟢 ATIVO</div>
            {% else %}
                <div class="big red">🔴 PAUSADO</div>
            {% endif %}
            <div class="small">Coleta continua mesmo pausado</div>
        </div>
        <div class="card">
            <div class="label">Placar</div>
            <div class="big">{{ status.motor.wins }}W / {{ status.motor.losses }}L</div>
            <div class="small">WHITE = LOSS</div>
        </div>
    </div>

    {% if status.motor.ativo and status.motor.sinal %}
        <div class="signal active">
            <div class="eyebrow">🎯 SINAL ATUAL • PRÓXIMA RODADA</div>
            {% if status.motor.cor == 'R' %}
                <div class="color red">🔴 VERMELHO</div>
            {% elif status.motor.cor == 'P' %}
                <div class="color black">⚫ PRETO</div>
            {% endif %}
            <div class="strategy">{{ status.motor.estrategia or 'Sinal ativo' }}</div>
            <div class="entry">
                Valor fixo: R$ {{ '%.2f'|format(status.motor.valor_aposta or 1.0) }}
                • Sem dobrar (Flat Betting)
            </div>
            <div class="entry">
                A entrada usa a cor exata da regra (Sem inversão).
                Em caso de perda, aguarda o próximo padrão.
            </div>
        </div>
    {% else %}
        <div class="signal">
            <div class="eyebrow">🎯 SINAL ATUAL</div>
            <div class="color" style="font-size:27px">⚪ AGUARDANDO GATILHO</div>
            <div class="strategy">
                {% if status.motor.ativo %}Nenhum padrão encontrado.{% else %}Motor pausado.{% endif %}
            </div>
            <div class="entry">A coleta permanece ativa independentemente do motor.</div>
        </div>
    {% endif %}

    <div class="section-title">
        <h2>📊 Desempenho</h2>
        <span>Sessão atual</span>
    </div>

    <div class="performance">
        <div class="metric">
            <div class="num green">{{ status.motor.wins }}</div>
            <div class="m-label">Acertos</div>
        </div>
        <div class="metric">
            <div class="num red">{{ status.motor.losses }}</div>
            <div class="m-label">Erros</div>
        </div>
        <div class="metric">
            <div class="num">{{ status.motor.wins + status.motor.losses }}</div>
            <div class="m-label">Sinais resolvidos</div>
        </div>
        <div class="metric">
            <div class="num yellow">
                {% set total_res = status.motor.wins + status.motor.losses %}
                {% if total_res > 0 %}
                    {{ '%.1f'|format(status.motor.wins / total_res * 100) }}%
                {% else %}
                    —
                {% endif %}
            </div>
            <div class="m-label">Taxa de acerto</div>
        </div>
    </div>

    <div class="section-title">
        <h2>🧾 Últimos sinais</h2>
        <span>Sessão atual</span>
    </div>

    <div class="history">
        <div class="history-row history-head">
            <div>Estratégia</div>
            <div>Entrada</div>
            <div>Rodada base</div>
            <div>Resultado</div>
            <div class="hide-mobile">Rodada resolvida</div>
        </div>

        {% if status.historico_sinais %}
            {% for s in status.historico_sinais %}
                <div class="history-row">
                    <div>{{ s.estrategia }}</div>
                    <div>
                        {% if s.cor_entrada == 'R' %}
                            <span class="red">🔴 R</span>
                        {% elif s.cor_entrada == 'P' %}
                            <span class="black">⚫ P</span>
                        {% endif %}
                        <div style="font-size:11px;color:#8993a1;margin-top:4px;">
                            {% if s.cor_regra %}
                                Regra: {{ s.cor_regra }} → Entrada: {{ s.cor_entrada }}
                            {% endif %}
                            • Aposta Fixa
                        </div>
                    </div>
                    <div>{{ s.base }}</div>
                    <div>
                        {% if s.resultado == 'WIN' %}
                            <span class="result-badge result-win">✓ WIN</span>
                        {% elif s.resultado == 'LOSS' %}
                            <span class="result-badge result-loss">✕ LOSS</span>
                        {% endif %}
                    </div>
                    <div class="hide-mobile">{{ s.rodada_resultado or '—' }}</div>
                </div>
            {% endfor %}
        {% else %}
            <div class="empty">Ainda não há sinais registrados nesta sessão.</div>
        {% endif %}
    </div>

    <div class="section-title">
        <h2>🎲 Últimos resultados</h2>
        <span>Rodadas da sessão</span>
    </div>

    <div class="results">
        {% if status.jogos %}
            {% for jogo in status.jogos %}
                <div class="pill {% if jogo.sigla == 'R' %}red-bg{% elif jogo.sigla == 'P' %}black-bg{% else %}white-bg{% endif %}"
                     title="{{ jogo.rodada }}">
                    {{ jogo.emoji }} {{ jogo.roll }}
                    <small>{{ jogo.sigla }}</small>
                </div>
            {% endfor %}
        {% else %}
            <div class="empty">Aguardando novas rodadas...</div>
        {% endif %}
    </div>

    <div class="footer">
        Blaze Bot V8.0 • Google Planilhas • coleta contínua •
        V49.0 HIT & RUN • WHITE = LOSS •
        proteção contra resolução antecipada
    </div>

</div>
</body>
</html>
"""

# ================================================================
# ROTA PRINCIPAL
# ================================================================

@app.route("/")
def home():
    return render_template_string(HTML, status=consultar_dashboard())


# ================================================================
# CONTROLE DO MOTOR
#
# PAUSAR: para somente novas previsões. O collector continua.
# INICIAR: abre uma NOVA SESSÃO estatística sem apagar histórico.
# ================================================================

@app.route("/controle_motor", methods=["POST"])
def controle_motor():
    try:
        estado = db.ler_estado()
        if not estado:
            return redirect("/")

        motor_ativo = db.to_bool(estado.get("motor_ativo"))

        if motor_ativo:
            print("⏸️ Motor pausado pelo dashboard. Collector continua.", flush=True)
            db.atualizar_estado({
                "motor_ativo": False,
                "atualizado_em": db.agora(),
            })
        else:
            print("▶️ Iniciando NOVA SESSÃO estatística.", flush=True)
            db.atualizar_estado({
                "motor_ativo": True,
                "inicio_sessao": db.agora(),
                "ciclo_ativo": False,
                "ciclo_id": "",
                "tentativa_atual": 0,
                "ciclo_cor_regra": "",
                "ciclo_cor_entrada": "",
                "ciclo_estrategia": "",
                "sinal_ativo": "",
                "cor_sinal": "",
                "ultima_estrategia": "",
                "rodada_base_sinal": "",
                "atualizado_em": db.agora(),
            })

        _DASH_CACHE["dados"] = None  # força atualização imediata do dashboard

    except Exception as e:
        print(f"❌ Erro ao alterar estado do motor: {e}", flush=True)

    return redirect("/")


# ================================================================
# HEALTH CHECK
# ================================================================

@app.route("/health")
def health():
    return jsonify({"status": "ok"}), 200


# ================================================================
# STATUS JSON
# ================================================================

@app.route("/status")
def status():
    return jsonify(consultar_dashboard()), 200


# ================================================================
# INICIAR COLLECTOR
# ================================================================

def iniciar_background_collector():
    try:
        try:
            from coletor import iniciar_coletor_em_thread
        except ImportError:
            from collector import iniciar_coletor_em_thread
        print("🚀 Iniciando Collector em background...", flush=True)
        iniciar_coletor_em_thread()
    except Exception as e:
        print(f"❌ Não foi possível iniciar o collector: {e}", flush=True)


# ================================================================
# INICIALIZAÇÃO
#
# O motor NÃO possui thread própria aqui.
# Ele é acionado pelo collector após cada rodada persistida.
# ================================================================

init_web_db()

try:
    from strategy_engine import init_engine_db
    init_engine_db()
except Exception as e:
    print(f"⚠️ Não foi possível inicializar strategy_engine: {e}", flush=True)

if os.getenv("RUN_COLLECTOR", "true").lower() == "true":
    collector_thread = threading.Thread(
        target=iniciar_background_collector,
        name="collector-bootstrap",
        daemon=True,
    )
    collector_thread.start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)
