import os
import requests
import psycopg2
from flask import Flask, redirect, render_template_string
from collections import deque

app = Flask(__name__)

APOSTA_BASE = 1.00
CORES = {0: "BRANCO", 1: "VERMELHO", 2: "PRETO"}
COR_SIGLA = {"BRANCO": "W", "VERMELHO": "R", "PRETO": "B"}

# ================================================================
# POSTGRESQL (NEON)
# ================================================================
def get_db_connection():
    database_url = os.getenv("DATABASE_URL")
    if not database_url: return None
    try:
        # Garante o SSL do Neon
        if "sslmode" not in database_url:
            database_url += "?sslmode=require"
        return psycopg2.connect(database_url, connect_timeout=10)
    except Exception as e:
        print(f"Erro Neon: {e}")
        return None

def init_db():
    conn = get_db_connection()
    if not conn: return
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS blaze_historico (
                    id SERIAL PRIMARY KEY,
                    rodada_id VARCHAR(100) UNIQUE NOT NULL,
                    color INTEGER, cor VARCHAR(20), roll INTEGER,
                    status VARCHAR(30), created_at TIMESTAMP
                );
            """)
            # Tabela que guarda o placar e o sinal atual do bot
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_estado (
                    id SERIAL PRIMARY KEY,
                    wins INTEGER DEFAULT 0,
                    losses INTEGER DEFAULT 0,
                    whites INTEGER DEFAULT 0,
                    profit NUMERIC(12,2) DEFAULT 0.0,
                    sinal_ativo VARCHAR(100),
                    cor_sinal VARCHAR(5),
                    ultima_rodada_processada VARCHAR(100)
                );
            """)
            # Insere uma linha inicial se a tabela estiver vazia
            cur.execute("SELECT COUNT(*) FROM bot_estado;")
            if cur.fetchone()[0] == 0:
                cur.execute("INSERT INTO bot_estado (wins, losses, whites, profit) VALUES (0,0,0,0);")
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Erro init_db: {e}")

# ================================================================
# MOTOR DAS 5 ESTRATÉGIAS
# ================================================================
def get_active_signal(rolls, colors):
    n = len(rolls)
    if n < 4: return None
    
    last_4 = list(colors)[-4:]
    if last_4 == ['R', 'R', 'B', 'B']: return ("EST 2 (Operacional)", "R")
    if last_4 == ['R', 'B', 'B', 'R']: return ("EST 2 (Operacional)", "R")
    if last_4 == ['B', 'B', 'R', 'R']: return ("EST 2 (Operacional)", "R")
    if last_4 == ['B', 'R', 'R', 'B']: return ("EST 2 (Operacional)", "B")
    
    if n >= 5:
        last_5 = list(colors)[-5:]
        if last_5 == ['R', 'R', 'R', 'B', 'R']: return ("EST 3 (Franco-Atirador)", "R")
        if last_5 == ['R', 'R', 'B', 'B', 'R']: return ("EST 3 (Franco-Atirador)", "R")
        if last_5 == ['B', 'R', 'B', 'R', 'R']: return ("EST 3 (Franco-Atirador)", "B")
        if last_5 == ['R', 'R', 'R', 'B', 'B']: return ("EST 3 (Franco-Atirador)", "R")
        
    if n >= 6:
        last_6_rolls = list(rolls)[-6:]
        last_6_colors = list(colors)[-6:]
        
        if all(r != 0 and r % 2 == 0 for r in last_6_rolls): return ("EST 1 (Sniper Par)", "R")
        if all(r != 0 and r % 2 != 0 for r in last_6_rolls): return ("EST 1 (Sniper Ímpar)", "B")
        
        if last_6_colors == ['B', 'B', 'R', 'B', 'R', 'R']: return ("EST 4 (Bala de Prata)", "B")
        if last_6_colors == ['B', 'R', 'R', 'R', 'B', 'B']: return ("EST 4 (Bala de Prata)", "R")
        if last_6_colors == ['R', 'R', 'R', 'B', 'B', 'R']: return ("EST 4 (Bala de Prata)", "R")
        if last_6_colors == ['R', 'R', 'B', 'B', 'B', 'B']: return ("EST 4 (Bala de Prata)", "B")
        
        if last_6_colors == ['R', 'B', 'R', 'R', 'B', 'B']: return ("EST 5 (Mina Oculta)", "B")
        if last_6_colors == ['B', 'R', 'B', 'R', 'B', 'B']: return ("EST 5 (Mina Oculta)", "B")
        if last_6_colors == ['R', 'R', 'B', 'R', 'R', 'B']: return ("EST 5 (Mina Oculta)", "B")
        if last_6_colors == ['R', 'B', 'B', 'R', 'R', 'R']: return ("EST 5 (Mina Oculta)", "R")
        if last_6_colors == ['B', 'B', 'B', 'R', 'B', 'R']: return ("EST 5 (Mina Oculta)", "R")
        if last_6_colors == ['R', 'R', 'R', 'R', 'B', 'R']: return ("EST 5 (Mina Oculta)", "R")
    return None

# ================================================================
# LÓGICA PRINCIPAL (Executada a cada 10s quando o site carrega)
# ================================================================
def processar_blaze():
    init_db()
    conn = get_db_connection()
    if not conn: return None, [], [], 0,0,0,0.0
    
    try:
        # 1. Pega os últimos 50 jogos da API da Blaze
        url = "https://api-gaming.blaze.bet.br/roulette_games/recent/1"
        resp = requests.get(url, timeout=8)
        if resp.status_code != 200: return None, [], [], 0,0,0,0.0
        
        jogos_blaze = resp.json()
        jogos_blaze.reverse() # Do mais antigo para o mais novo
        
        with conn.cursor() as cur:
            # Pega o estado atual do bot
            cur.execute("SELECT wins, losses, whites, profit, sinal_ativo, cor_sinal, ultima_rodada_processada FROM bot_estado WHERE id=1;")
            estado = cur.fetchone()
            wins, losses, whites, profit, sinal_ativo, cor_sinal, ultima_processada = estado
            
            rolls_mem = []
            colors_mem = []
            novos_jogos = False
            
            # 2. Salva novos jogos no Neon e atualiza memória
            for jogo in jogos_blaze:
                rodada_id = str(jogo.get("id"))
                roll = int(jogo.get("roll"))
                color_int = int(jogo.get("color"))
                cor_nome = CORES.get(color_int)
                sigla = COR_SIGLA.get(cor_nome)
                
                cur.execute("""
                    INSERT INTO blaze_historico (rodada_id, color, cor, roll, status, created_at)
                    VALUES (%s,%s,%s,%s,'complete', NOW()) ON CONFLICT (rodada_id) DO NOTHING RETURNING id;
                """, (rodada_id, color_int, cor_nome, roll))
                inserido = cur.fetchone()
                
                # Adiciona na memória apenas jogos que já estavam no banco ou são novos
                if inserido or rodada_id == ultima_processada or len(rolls_mem) > 0:
                    rolls_mem.append(roll)
                    colors_mem.append(sigla)
                
                # Se for a rodada que acabou de ser processada, começa a coletar dali pra frente
                if rodada_id == ultima_processada:
                    rolls_mem = [roll]
                    colors_mem = [sigla]
            
            # Pega exatamente os últimos 10 da memória
            rolls_recentes = rolls_mem[-10:]
            colors_recentes = colors_mem[-10:]
            
            # 3. Se houver sinal ativo e a próxima rodada fechou, contabiliza
            if sinal_ativo and len(rolls_mem) > 0:
                # O último jogo da lista é o que fechou após o sinal
                result_roll = rolls_mem[-1]
                result_sigla = colors_mem[-1]
                
                if result_sigla == "W":
                    whites += 1; profit -= APOSTA_BASE
                elif result_sigla == cor_sinal:
                    wins += 1; profit += APOSTA_BASE
                else:
                    losses += 1; profit -= APOSTA_BASE
                
                sinal_ativo = None # Reseta o sinal
            
            # 4. Verifica as estratégias para a PRÓXIMA rodada
            # Usamos todos exceto o último (que já foi avaliado acima)
            r_check = rolls_mem[:-1] if sinal_ativo is None and len(rolls_mem)>0 else rolls_mem
            c_check = colors_mem[:-1] if sinal_ativo is None and len(rolls_mem)>0 else colors_mem
            
            novo_sinal = get_active_signal(r_check, c_check)
            if novo_sinal:
                sinal_ativo = novo_sinal[0]
                cor_sinal = novo_sinal[1]
                ultima_processada = str(jogos_blaze[-1]["id"]) # Marca que vai aguardar a próxima
            else:
                sinal_ativo = None
                ultima_processada = str(jogos_blaze[-1]["id"])
            
            # 5. Atualiza o banco
            cur.execute("""
                UPDATE bot_estado SET wins=%s, losses=%s, whites=%s, profit=%s, 
                sinal_ativo=%s, cor_sinal=%s, ultima_rodada_processada=%s WHERE id=1;
            """, (wins, losses, whites, profit, sinal_ativo, cor_sinal, ultima_processada))
            
        conn.commit()
        conn.close()
        
        return (sinal_ativo, cor_sinal), rolls_recentes, colors_recentes, wins, losses, whites, profit
    except Exception as e:
        print(f"Erro processar: {e}")
        try: conn.close()
        except: pass
        return None, [], [], 0,0,0,0.0

# ================================================================
# HTML TEMPLATE
# ================================================================
HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="pt-br">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="10">
<title>Robô 5 Estratégias - Blaze</title>
<style>
:root { --bg: #07090d; --card: #11151c; --border: rgba(255,255,255,.08); --text: #e8edf3; --muted: #7d8794; --red: #ff4d57; --green: #19df78; --white: #f1f3f5; --yellow: #ffc247; }
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: radial-gradient(circle at top, #171d28 0%, var(--bg) 52%); color: var(--text); font-family: Arial, sans-serif; min-height: 100vh; padding: 18px; }
.container { max-width: 1180px; margin: auto; }
.header { display: flex; justify-content: space-between; align-items: center; gap: 15px; margin-bottom: 15px; }
.logo { font-size: 25px; font-weight: 900; } .logo span { color: var(--red); }
.status-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-bottom: 12px; }
.status-card { background: rgba(17,21,28,.86); border: 1px solid var(--border); border-radius: 14px; padding: 15px; }
.status-title { color: var(--muted); text-transform: uppercase; font-size: 10px; font-weight: 800; margin-bottom: 8px; }
.status-value { font-size: 15px; font-weight: 900; }
.online { color: var(--green); }
.signal-box { margin-bottom: 12px; border-radius: 16px; padding: 20px; text-align: center; background: var(--card); border: 1px solid var(--border); }
.signal-red { color: var(--red); font-size: 31px; font-weight: 1000; }
.signal-black { color: #dce1e7; font-size: 31px; font-weight: 1000; }
.signal-none { color: var(--yellow); font-size: 20px; font-weight: 900; }
.signal-detail { margin-top: 7px; color: var(--muted); font-size: 13px; }
.stats { display: grid; grid-template-columns: repeat(5, 1fr); gap: 10px; margin-bottom: 12px; }
.card { background: rgba(17,21,28,.86); border: 1px solid var(--border); border-radius: 14px; padding: 15px; }
.value { font-size: 26px; font-weight: 950; }
.green { color: var(--green); } .red { color: var(--red); } .yellow { color: var(--yellow); } .white { color: var(--white); }
.profit-positive { color: var(--green); } .profit-negative { color: var(--red); }
.trend { display: flex; gap: 6px; overflow-x: auto; padding: 10px; margin-bottom: 12px; background: var(--card); border: 1px solid var(--border); border-radius: 14px; }
.pill { min-width: 38px; height: 38px; border-radius: 9px; display: flex; align-items: center; justify-content: center; font-weight: 900; flex-shrink: 0; }
.pill-r { background: var(--red); color: white; } .pill-b { background: #252a31; color: white; border: 1px solid #454c55; } .pill-w { background: white; color: #111; }
</style>
</head>
<body>
<div class="container">
    <div class="header"><div class="logo">🤖 Robô <span>5 Estratégias</span></div></div>
    <div class="status-grid">
        <div class="status-card"><div class="status-title">Status</div><div class="status-value online">🟢 ATIVO</div></div>
        <div class="status-card"><div class="status-title">Atualização</div><div class="status-value">10s</div></div>
        <div class="status-card"><div class="status-title">Aposta Base</div><div class="status-value">R$ {{ '%.2f'|format(bet_amount) }}</div></div>
        <div class="status-card"><div class="status-title">Win Rate</div><div class="value yellow">{{ win_rate }}%</div></div>
    </div>
    <div class="signal-box">
        {% if active_signal %}
            {% if active_signal[1] == 'R' %}<div class="signal-red">🔴 ENTRAR NO VERMELHO</div>
            {% else %}<div class="signal-black">⚫ ENTRAR NO PRETO</div>{% endif %}
            <div class="signal-detail">Gatilho: <strong>{{ active_signal[0] }}</strong> | Entrada Seca (1 tentativa)</div>
        {% else %}
            <div class="signal-none">🎯 AGUARDANDO GATILHO...</div>
            <div class="signal-detail">Monitorando as 5 estratégias em tempo real</div>
        {% endif %}
    </div>
    <div class="stats">
        <div class="card"><div class="status-title">Saldo</div><div class="value {{ 'profit-positive' if profit >= 0 else 'profit-negative' }}">R$ {{ '%.2f'|format(profit) }}</div></div>
        <div class="card"><div class="status-title">Wins</div><div class="value green">{{ wins }}</div></div>
        <div class="card"><div class="status-title">Losses</div><div class="value red">{{ losses }}</div></div>
        <div class="card"><div class="status-title">Whites</div><div class="value white">{{ whites }}</div></div>
        <div class="card"><div class="status-title">Total Ops</div><div class="value">{{ total_ops }}</div></div>
    </div>
    <div class="trend">
        {% for i in range(numbers|length) %}<div class="pill pill-{{ colors[i].lower() }}">{{ numbers[i] }}</div>{% endfor %}
    </div>
</div>
</body>
</html>
"""

# ================================================================
# ROTAS WEB
# ================================================================
@app.route("/")
def home():
    signal, rolls, colors, wins, losses, whites, profit = processar_blaze()
    
    total_ops = wins + losses + whites
    win_rate = round(wins / total_ops * 100, 1) if total_ops > 0 else 0.0
    
    numbers_display = list(reversed(rolls))
    colors_display = list(reversed(colors))
    
    return render_template_string(
        HTML_TEMPLATE,
        active_signal=signal,
        numbers=numbers_display, colors=colors_display,
        wins=wins, losses=losses, whites=whites, profit=profit,
        win_rate=win_rate, total_ops=total_ops, bet_amount=APOSTA_BASE
    )

@app.route("/health")
def health():
    return {"status": "ok"}, 200
