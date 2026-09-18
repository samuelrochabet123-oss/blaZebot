import os
import requests
from datetime import datetime, timedelta
import psycopg2

# ================================================================
# V49.0 HIT & RUN + TELEGRAM + AUTO-STOP META +3
# ================================================================

APOSTA_BASE = 1.00
MAX_TENTATIVAS = 1
INVERSAO_ATIVA = False
META_DIARIA = 3

CORES = {0: "BRANCO", 1: "VERMELHO", 2: "PRETO"}
COR_SIGLA = {"BRANCO": "W", "VERMELHO": "R", "PRETO": "P"}

RESULTADO_PENDENTE = "PENDENTE"
RESULTADO_WIN = "WIN"
RESULTADO_LOSS = "LOSS"

# ================================================================
# TELEGRAM
# ================================================================
def enviar_telegram(mensagem):
    token = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id: return
    
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = {"chat_id": chat_id, "text": mensagem, "parse_mode": "Markdown"}
    try:
        requests.post(url, data=data, timeout=5)
    except:
        pass

# ================================================================
# FILTRO DE HORÁRIO E META (HIT & RUN - 13h às 14h BRT)
# ================================================================
def get_hora_brt():
    return (datetime.utcnow() - timedelta(hours=3)).hour

def get_data_brt():
    return (datetime.utcnow() - timedelta(hours=3)).date()

def is_horario_permitido():
    return get_hora_brt() in [13, 14]

# ================================================================
# BANCO DE DADOS
# ================================================================
def get_db_connection():
    database_url = os.getenv("DATABASE_URL")
    if not database_url: return None
    try:
        if "sslmode=" not in database_url:
            separator = "&" if "?" in database_url else "?"
            database_url = f"{database_url}{separator}sslmode=require"
        return psycopg2.connect(database_url, connect_timeout=15)
    except Exception as e:
        print(f"❌ MOTOR: erro PostgreSQL: {e}", flush=True)
        return None

def init_engine_db():
    conn = get_db_connection()
    if not conn: return False
    try:
        with conn.cursor() as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS bot_estado (
                id INTEGER PRIMARY KEY, wins INTEGER DEFAULT 0, losses INTEGER DEFAULT 0, whites INTEGER DEFAULT 0,
                profit NUMERIC(12,2) DEFAULT 0.00, sinal_ativo VARCHAR(150), cor_sinal VARCHAR(5),
                ultima_rodada_processada VARCHAR(100), rodada_base_sinal VARCHAR(100), motor_ativo BOOLEAN DEFAULT FALSE,
                ultima_estrategia VARCHAR(150), inicio_sessao TIMESTAMP, ciclo_ativo BOOLEAN DEFAULT FALSE, ciclo_id VARCHAR(120),
                tentativa_atual INTEGER DEFAULT 0, ciclo_cor_regra VARCHAR(5), ciclo_cor_entrada VARCHAR(5), ciclo_estrategia VARCHAR(150),
                atualizado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP);""")
            for name, definition in [("wins", "INTEGER DEFAULT 0"), ("losses", "INTEGER DEFAULT 0"), ("whites", "INTEGER DEFAULT 0"), ("profit", "NUMERIC(12,2) DEFAULT 0.00"), ("sinal_ativo", "VARCHAR(150)"), ("cor_sinal", "VARCHAR(5)"), ("ultima_rodada_processada", "VARCHAR(100)"), ("rodada_base_sinal", "VARCHAR(100)"), ("motor_ativo", "BOOLEAN DEFAULT FALSE"), ("ultima_estrategia", "VARCHAR(150)"), ("inicio_sessao", "TIMESTAMP"), ("ciclo_ativo", "BOOLEAN DEFAULT FALSE"), ("ciclo_id", "VARCHAR(120)"), ("tentativa_atual", "INTEGER DEFAULT 0"), ("ciclo_cor_regra", "VARCHAR(5)"), ("ciclo_cor_entrada", "VARCHAR(5)"), ("ciclo_estrategia", "VARCHAR(150)"), ("atualizado_em", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP")]:
                cur.execute(f"ALTER TABLE bot_estado ADD COLUMN IF NOT EXISTS {name} {definition};")
            cur.execute("""DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'bot_estado' AND column_name = 'sinal_ativo' AND data_type = 'boolean') THEN ALTER TABLE bot_estado ALTER COLUMN sinal_ativo TYPE VARCHAR(150) USING CASE WHEN sinal_ativo THEN 'ATIVO' ELSE NULL END; END IF; END $$;""")
            cur.execute("INSERT INTO bot_estado (id, wins, losses, whites, profit, motor_ativo, ciclo_ativo, tentativa_atual) VALUES (1, 0, 0, 0, 0.00, FALSE, FALSE, 0) ON CONFLICT (id) DO NOTHING;")
            cur.execute("""CREATE TABLE IF NOT EXISTS estrategia_sinais (
                id SERIAL PRIMARY KEY, rodada_base VARCHAR(100) NOT NULL, estrategia VARCHAR(150) NOT NULL, cor_prevista VARCHAR(5) NOT NULL,
                rodada_resultado VARCHAR(100), cor_resultado VARCHAR(5), resultado VARCHAR(20), criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                resolvido_em TIMESTAMP, tentativa INTEGER DEFAULT 1, ciclo_id VARCHAR(120), cor_regra VARCHAR(5), cor_entrada VARCHAR(5), valor_aposta NUMERIC(12,2) DEFAULT 1.00);""")
            for name, definition in [("tentativa", "INTEGER DEFAULT 1"), ("ciclo_id", "VARCHAR(120)"), ("cor_regra", "VARCHAR(5)"), ("cor_entrada", "VARCHAR(5)"), ("valor_aposta", "NUMERIC(12,2) DEFAULT 1.00")]:
                cur.execute(f"ALTER TABLE estrategia_sinais ADD COLUMN IF NOT EXISTS {name} {definition};")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_estrategia_sinais_base ON estrategia_sinais(rodada_base);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_estrategia_sinais_estrategia ON estrategia_sinais(estrategia);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_estrategia_sinais_resultado ON estrategia_sinais(resultado);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_estrategia_sinais_criado_em ON estrategia_sinais(criado_em);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_estrategia_sinais_ciclo ON estrategia_sinais(ciclo_id);")
            cur.execute("UPDATE estrategia_sinais SET resultado = 'LOSS' WHERE resultado = 'WHITE';")
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"❌ MOTOR: erro inicializando banco: {e}", flush=True)
        try: conn.rollback(); conn.close()
        except: pass
        return False

def cor_para_sigla(color):
    try: return COR_SIGLA.get(CORES.get(int(color)))
    except (TypeError, ValueError): return None

def mapear_cor_texto(cor):
    if cor is None: return None
    texto = str(cor).upper().strip()
    if texto in {"R", "RED", "VERMELHO", "V", "VI"} or "VERMELHO" in texto: return "R"
    if texto in {"P", "BLACK", "PRETO", "B"} or "PRETO" in texto: return "P"
    if texto in {"W", "WHITE", "BRANCO"} or "BRANCO" in texto: return "W"
    return None

def obter_cor(cor_texto, color):
    return mapear_cor_texto(cor_texto) or cor_para_sigla(color)

def carregar_historico_por_cursor(cur, ate_id=None):
    query = "SELECT id, rodada_id, color, cor, roll FROM blaze_historico WHERE status = 'complete' AND color IN (0, 1, 2) AND roll IS NOT NULL"
    params = []
    if ate_id is not None: query += " AND id <= %s"; params.append(int(ate_id))
    query += " ORDER BY id ASC;"
    cur.execute(query, params)
    historico = []
    for db_id, rodada_id, color, cor_texto, roll in cur.fetchall():
        cor = obter_cor(cor_texto, color)
        if cor is None: continue
        try: roll = int(roll)
        except (TypeError, ValueError): continue
        historico.append({"id": int(db_id), "rodada_id": str(rodada_id), "cor": cor, "roll": roll})
    return historico

def get_lucro_dia(cur):
    data_hoje = get_data_brt()
    # Calcula o lucro de hoje baseado na tabela de sinais (convertendo a data UTC para BRT)
    cur.execute("""
        SELECT COALESCE(SUM(CASE WHEN resultado = 'WIN' THEN 1 WHEN resultado = 'LOSS' THEN -1 ELSE 0 END), 0) 
        FROM estrategia_sinais 
        WHERE (criado_em - interval '3 hours')::date = %s
    """, (data_hoje,))
    return cur.fetchone()[0]

def detectar_estrategia(cur, hist):
    if not hist or len(hist) < 2: return None, None
    
    # 1. CHECA O FILTRO DE HORÁRIO
    if not is_horario_permitido():
        return None, None

    # 2. CHECA SE JÁ BATEU A META DO DIA
    if get_lucro_dia(cur) >= META_DIARIA:
        return None, None

    # 3. ESTRATÉGIA V46 (Branco -> Inversão)
    cor_atual = hist[-1]["cor"]
    cor_anterior = hist[-2]["cor"]
    
    if cor_atual == "W" and cor_anterior in ["R", "P"]:
        cor_entrada = "P" if cor_anterior == "R" else "R"
        return cor_entrada, "V49.0 HIT & RUN | B -> Inversão"

    return None, None

def inverter_cor(cor):
    if cor == "R": return "P"
    if cor == "P": return "R"
    return cor

def _resultado_do_sinal(cor_entrada, cor_resultado):
    return RESULTADO_WIN if cor_resultado == cor_entrada else RESULTADO_LOSS

def _proxima_rodada_complete(cur, base_db_id):
    cur.execute("SELECT id, rodada_id, color, cor, roll FROM blaze_historico WHERE status = 'complete' AND id > %s AND color IN (0, 1, 2) AND roll IS NOT NULL ORDER BY id ASC LIMIT 1;", (base_db_id,))
    return cur.fetchone()

def resolver_sinal_por_rodada(cur, sinal_id, rodada_base, estrategia, cor_entrada, rodada_resultado, cor_resultado, now):
    resultado = _resultado_do_sinal(cor_entrada, cor_resultado)
    cur.execute("UPDATE estrategia_sinais SET rodada_resultado = %s, cor_resultado = %s, resultado = %s, resolvido_em = %s WHERE id = %s AND resultado = 'PENDENTE';", (str(rodada_resultado), cor_resultado, resultado, now, sinal_id))
    if cur.rowcount != 1: return False
    print(f"📊 SINAL RESOLVIDO | id={sinal_id} | base={rodada_base} | resultado={rodada_resultado} | estratégia={estrategia} | entrada={cor_entrada} | real={cor_resultado} | {resultado}", flush=True)
    return True

def _obter_ciclo(cur):
    cur.execute("SELECT ciclo_ativo, ciclo_id, tentativa_atual, ciclo_cor_regra, ciclo_cor_entrada, ciclo_estrategia FROM bot_estado WHERE id = 1;")
    row = cur.fetchone()
    if not row: return {"ativo": False, "id": None, "tentativa": 0, "cor_regra": None, "cor_entrada": None, "estrategia": None}
    return {"ativo": bool(row[0]), "id": row[1], "tentativa": int(row[2] or 0), "cor_regra": row[3], "cor_entrada": row[4], "estrategia": row[5]}

def _finalizar_ciclo(cur, now, motivo):
    cur.execute("UPDATE bot_estado SET ciclo_ativo = FALSE, ciclo_id = NULL, tentativa_atual = 0, ciclo_cor_regra = NULL, ciclo_cor_entrada = NULL, ciclo_estrategia = NULL, sinal_ativo = NULL, cor_sinal = NULL, rodada_base_sinal = NULL, atualizado_em = %s WHERE id = 1;", (now,))
    print(f"🏁 CICLO ENCERRADO | {motivo}", flush=True)
    
    # Envia resultado para o Telegram
    lucro = get_lucro_dia(cur)
    if "WIN" in motivo:
        msg = f"✅ *WIN!*\n💰 Lucro de hoje: {lucro:+}\n🎯 Meta: +{META_DIARIA}"
    else:
        msg = f"❌ *LOSS!*\n💰 Lucro de hoje: {lucro:+}\n🎯 Meta: +{META_DIARIA}"
    enviar_telegram(msg)
    
    # Se bateu a meta, avisa no Telegram
    if lucro >= META_DIARIA:
        enviar_telegram(f"🛑 *META DIÁRIA BATIDA!*\nO bot foi desligado e só volta a operar amanhã às 13h (Horário de Brasília).")

def _criar_tentativa(cur, rodada_base_id, rodada_base, ciclo_id, tentativa, estrategia, cor_regra, cor_entrada, now):
    cur.execute("INSERT INTO estrategia_sinais (rodada_base, estrategia, cor_prevista, resultado, tentativa, ciclo_id, cor_regra, cor_entrada, valor_aposta) VALUES (%s, %s, %s, 'PENDENTE', %s, %s, %s, %s, %s);", (str(rodada_base), estrategia, cor_entrada, int(tentativa), str(ciclo_id), cor_regra, cor_entrada, APOSTA_BASE))
    cur.execute("UPDATE bot_estado SET ciclo_ativo = TRUE, ciclo_id = %s, tentativa_atual = %s, ciclo_cor_regra = %s, ciclo_cor_entrada = %s, ciclo_estrategia = %s, sinal_ativo = %s, cor_sinal = %s, rodada_base_sinal = %s, ultima_estrategia = %s, atualizado_em = %s WHERE id = 1;", (str(ciclo_id), int(tentativa), cor_regra, cor_entrada, estrategia, estrategia, cor_entrada, str(rodada_base), estrategia, now))
    
    nome_cor = "VERMELHO" if cor_entrada == "R" else "PRETO"
    print("\n" + "=" * 72)
    print(f"🎯 NOVA ENTRADA V49.0 | TENTATIVA {tentativa}/{MAX_TENTATIVAS} | ENTRADA NO {nome_cor}")
    print("=" * 72 + "\n")
    
    # Envia o Sinal para o Telegram
    msg = f"🚨 *SINAL DETECTADO* 🚨\n\n👉 Entrada no: *{nome_cor}*\n💵 Valor: R$ {APOSTA_BASE:.2f}\n⏰ Janela: 13h - 14h"
    enviar_telegram(msg)

def _criar_primeiro_ciclo(cur, rodada_id, rodada_db_id, now):
    historico = carregar_historico_por_cursor(cur, ate_id=rodada_db_id)
    cor_regra, estrategia = detectar_estrategia(cur, historico)
    if not estrategia: return False
    
    cur.execute("SELECT id FROM estrategia_sinais WHERE rodada_base = %s ORDER BY id DESC LIMIT 1;", (str(rodada_id),))
    if cur.fetchone(): return False
    
    ciclo_id = f"{rodada_id}-{int(datetime.now().timestamp() * 1000)}"
    cor_entrada = inverter_cor(cor_regra) if INVERSAO_ATIVA else cor_regra
    _criar_tentativa(cur, rodada_db_id, rodada_id, ciclo_id, 1, estrategia, cor_regra, cor_entrada, now)
    return True

def _resolver_atual_e_avancar_ciclo(cur, atual_id, now):
    cur.execute("""SELECT s.id, s.rodada_base, s.estrategia, s.cor_prevista, s.tentativa, s.ciclo_id, s.cor_regra, s.cor_entrada, b.id AS base_db_id
                   FROM estrategia_sinais s JOIN blaze_historico b ON b.rodada_id = s.rodada_base AND b.status = 'complete'
                   WHERE s.resultado = 'PENDENTE' AND b.id < %s ORDER BY s.id ASC;""", (atual_id,))
    for row in cur.fetchall():
        sinal_id, rodada_base, estrategia, cor_prevista, tentativa, ciclo_id, cor_regra, cor_entrada, base_db_id = row
        rodada = _proxima_rodada_complete(cur, int(base_db_id))
        if not rodada or int(rodada[0]) != int(atual_id): continue
        _, rodada_resultado, color_resultado, cor_texto, _roll = rodada
        cor_resultado = obter_cor(cor_texto, color_resultado)
        if cor_resultado is None: continue
        if not resolver_sinal_por_rodada(cur, sinal_id, rodada_base, estrategia, cor_prevista, rodada_resultado, cor_resultado, now): continue
        resultado = _resultado_do_sinal(cor_prevista, cor_resultado)
        ciclo = _obter_ciclo(cur)
        if ciclo["ativo"] and ciclo["id"] == ciclo_id:
            if resultado == RESULTADO_WIN:
                _finalizar_ciclo(cur, now, f"WIN na tentativa {tentativa}/{MAX_TENTATIVAS}")
            elif int(tentativa) < MAX_TENTATIVAS:
                proxima_tentativa = int(tentativa) + 1
                _criar_tentativa(cur, atual_id, rodada_resultado, ciclo_id, proxima_tentativa, estrategia, cor_regra, cor_entrada, now)
            else:
                _finalizar_ciclo(cur, now, "LOSS - Ciclo encerrado (Aposta Fixa)")

def reconciliar_sinais_pendentes(cur, now, limite=5000):
    cur.execute("""SELECT s.id, s.rodada_base, s.estrategia, s.cor_prevista, b.id AS base_db_id
                   FROM estrategia_sinais s JOIN blaze_historico b ON b.rodada_id = s.rodada_base AND b.status = 'complete'
                   WHERE s.resultado = 'PENDENTE' ORDER BY s.id ASC LIMIT %s;""", (limite,))
    resolvidos = 0
    for sinal_id, rodada_base, estrategia, cor_prevista, base_db_id in cur.fetchall():
        row = _proxima_rodada_complete(cur, int(base_db_id))
        if not row: continue
        _, rodada_resultado, color_resultado, cor_texto, _roll = row
        cor_resultado = obter_cor(cor_texto, color_resultado)
        if cor_resultado is None: continue
        if resolver_sinal_por_rodada(cur, sinal_id, rodada_base, estrategia, cor_prevista, rodada_resultado, cor_resultado, now): resolvidos += 1
    return resolvidos

def recalcular_bot_estado(cur, rodada_atual=None, now=None):
    cur.execute("""SELECT COUNT(*) FILTER (WHERE resultado = 'WIN') AS wins, COUNT(*) FILTER (WHERE resultado = 'LOSS') AS losses,
                   COUNT(*) FILTER (WHERE resultado = 'PENDENTE') AS pendentes, COUNT(*) FILTER (WHERE resultado = 'LOSS' AND cor_resultado = 'W') AS whites_internos,
                   COALESCE(SUM(CASE WHEN resultado = 'WIN' THEN valor_aposta WHEN resultado = 'LOSS' THEN -valor_aposta ELSE 0 END), 0) AS profit FROM estrategia_sinais;""")
    wins, losses, pendentes, whites_internos, profit = cur.fetchone()
    wins, losses, pendentes, whites_internos, profit = int(wins or 0), int(losses or 0), int(pendentes or 0), int(whites_internos or 0), float(profit or 0.0)
    cur.execute("SELECT motor_ativo, inicio_sessao FROM bot_estado WHERE id = 1;")
    state = cur.fetchone()
    motor_ativo = bool(state[0]) if state else False
    ciclo = _obter_ciclo(cur)
    rodada_base_sinal = None
    if ciclo["ativo"] and ciclo["id"]:
        cur.execute("SELECT rodada_base FROM estrategia_sinais WHERE ciclo_id = %s AND resultado = 'PENDENTE' ORDER BY id DESC LIMIT 1;", (str(ciclo["id"],)))
        base_row = cur.fetchone()
        rodada_base_sinal = base_row[0] if base_row else None
    now = now or datetime.now()
    cur.execute("""UPDATE bot_estado SET wins = %s, losses = %s, whites = %s, profit = %s, sinal_ativo = %s, cor_sinal = %s,
                   ultima_rodada_processada = COALESCE(%s, ultima_rodada_processada), rodada_base_sinal = %s, ultima_estrategia = %s, atualizado_em = %s WHERE id = 1;""",
                (wins, losses, whites_internos, profit, ciclo["estrategia"] if ciclo["ativo"] else None, ciclo["cor_entrada"] if ciclo["ativo"] else None, str(rodada_atual) if rodada_atual is not None else None, rodada_base_sinal, ciclo["estrategia"] if ciclo["ativo"] else None, now))
    return {"wins": wins, "losses": losses, "pendentes": pendentes, "whites_internos": whites_internos, "profit": profit, "sinal_ativo": ciclo["estrategia"] if ciclo["ativo"] else None, "cor_sinal": ciclo["cor_entrada"] if ciclo["ativo"] else None, "rodada_base_sinal": None, "estrategia": ciclo["estrategia"] if ciclo["ativo"] else None, "motor_ativo": motor_ativo, "ciclo_ativo": ciclo["ativo"], "ciclo_id": ciclo["id"], "tentativa_atual": ciclo["tentativa"], "ciclo_cor_regra": ciclo["cor_regra"], "ciclo_cor_entrada": ciclo["cor_entrada"]}

def _rodada_persistida(cur, rodada_id):
    cur.execute("SELECT id, color, cor, roll FROM blaze_historico WHERE rodada_id = %s AND status = 'complete' LIMIT 1;", (str(rodada_id),))
    return cur.fetchone()

def processar_novo_resultado(rodada_id, color, roll):
    if not init_engine_db(): return None
    conn = get_db_connection()
    if not conn: return None
    try:
        rodada_id, color, roll = str(rodada_id), int(color), int(roll)
        cor_atual = cor_para_sigla(color)
        if cor_atual is None: print(f"⚠️ MOTOR: cor inválida na rodada {rodada_id}: {color}", flush=True); conn.close(); return None
        now = datetime.now()
        with conn.cursor() as cur:
            rodada = _rodada_persistida(cur, rodada_id)
            if not rodada: raise RuntimeError(f"Rodada {rodada_id} não encontrada como complete.")
            rodada_db_id = int(rodada[0])
            cur.execute("SELECT motor_ativo, ultima_rodada_processada FROM bot_estado WHERE id = 1;")
            estado = cur.fetchone()
            motor_ativo = bool(estado[0]) if estado else False
            ultima_processada = str(estado[1]) if estado and estado[1] is not None else None
            if ultima_processada == rodada_id:
                resumo = recalcular_bot_estado(cur, rodada_id, now)
                conn.commit()
                return {"ativo": motor_ativo, "estrategia": resumo["estrategia"], "cor": resumo["cor_sinal"], "wins": resumo["wins"], "losses": resumo["losses"], "pendentes": resumo["pendentes"], "profit": resumo["profit"]}
            cur.execute("UPDATE bot_estado SET ultima_rodada_processada = %s, atualizado_em = %s WHERE id = 1;", (rodada_id, now))
            ciclo_antes = _obter_ciclo(cur)
            if motor_ativo or ciclo_antes["ativo"]:
                _resolver_atual_e_avancar_ciclo(cur, rodada_db_id, now)
                ciclo = _obter_ciclo(cur)
                if motor_ativo and not ciclo["ativo"]:
                    _criar_primeiro_ciclo(cur, rodada_id, rodada_db_id, now)
            else:
                print(f"⏸️ MOTOR V49.0 PAUSADO | rodada={rodada_id} | coleta registrada, nenhuma nova previsão criada", flush=True)
            reconciliar_sinais_pendentes(cur, now, limite=5000)
            resumo = recalcular_bot_estado(cur, rodada_id, now)
        conn.commit()
        conn.close()
        return {"ativo": motor_ativo, "estrategia": resumo["estrategia"], "cor": resumo["cor_sinal"], "wins": resumo["wins"], "losses": resumo["losses"], "pendentes": resumo["pendentes"], "profit": resumo["profit"]}
    except Exception as e:
        print(f"❌ MOTOR V49.0: erro processando rodada {rodada_id}: {e}", flush=True)
        try: conn.rollback(); conn.close()
        except: pass
        return None

def reconciliar_todos_sinais():
    if not init_engine_db(): return None
    conn = get_db_connection()
    if not conn: return None
    try:
        now = datetime.now()
        with conn.cursor() as cur:
            resolvidos = reconciliar_sinais_pendentes(cur, now, limite=50000)
            resumo = recalcular_bot_estado(cur, now=now)
        conn.commit()
        conn.close()
        print(f"🧾 AUDITORIA V49.0 | resolvidos={resolvidos} | pendentes={resumo['pendentes']} | W={resumo['wins']} | L={resumo['losses']} | profit={resumo['profit']:.2f}", flush=True)
        return {"resolvidos": resolvidos, **resumo}
    except Exception as e:
        print(f"❌ AUDITORIA V49.0: erro: {e}", flush=True)
        try: conn.rollback(); conn.close()
        except: pass
        return None

def obter_status_motor():
    conn = get_db_connection()
    if not conn: return {"ativo": False, "sinal": None, "cor": None, "estrategia": None, "wins": 0, "losses": 0, "pendentes": 0, "profit": 0.0, "ciclo_ativo": False, "tentativa_atual": 0}
    try:
        with conn.cursor() as cur:
            resumo = recalcular_bot_estado(cur, now=datetime.now())
        conn.commit()
        conn.close()
        return {"ativo": resumo["motor_ativo"], "sinal": resumo["sinal_ativo"], "cor": resumo["cor_sinal"], "estrategia": resumo["estrategia"], "wins": resumo["wins"], "losses": resumo["losses"], "pendentes": resumo["pendentes"], "profit": resumo["profit"], "ciclo_ativo": resumo["ciclo_ativo"], "tentativa_atual": resumo["tentativa_atual"], "ciclo_cor_regra": resumo["ciclo_cor_regra"], "ciclo_cor_entrada": resumo["ciclo_cor_entrada"]}
    except Exception as e:
        print(f"❌ MOTOR: erro consultando status: {e}", flush=True)
        try: conn.rollback(); conn.close()
        except: pass
        return {"ativo": False, "sinal": None, "cor": None, "estrategia": None, "wins": 0, "losses": 0, "pendentes": 0, "profit": 0.0, "ciclo_ativo": False, "tentativa_atual": 0}
