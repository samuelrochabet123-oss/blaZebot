# ================================================================
# BLAZE BOT — DASHBOARD WEB + COLLECTOR + MOTOR ESTATÍSTICO
#
# VERSÃO: V8.5 MG3 — SEIS REGRAS + INVERSÃO + ATÉ 3 TENTATIVAS
#
# Estratégias ativas, todas definidas em strategy_engine.py:
#   1) VERMELHO -> BRANCO -> PRETO -> VERMELHO -> PRETO
#   2) PRETO -> BRANCO -> VERMELHO -> PRETO -> VERMELHO
#   3) VERMELHO -> BRANCO -> VERMELHO -> PRETO -> PRETO
#   4) PRETO -> BRANCO -> PRETO -> PRETO -> PRETO
#   5) PRETO -> BRANCO -> VERMELHO -> VERMELHO -> PRETO
#   6) VERMELHO -> BRANCO -> VERMELHO -> VERMELHO -> VERMELHO
#
# A última cor de cada sequência é a previsão para a PRÓXIMA rodada.
#
# CORREÇÕES IMPORTANTES:
#
# 1. O sinal é baseado em uma rodada específica.
# 2. O resultado só pode ser uma rodada POSTERIOR ao sinal_base_id.
# 3. A própria rodada que gerou o sinal NUNCA pode resolver o sinal.
# 4. Branco na rodada de entrada é LOSS operacional.
# 5. Dashboard não exibe WHITE como categoria de resultado.
# 6. O collector continua coletando mesmo com o motor parado.
# 7. INICIAR cria uma nova sessão estatística.
# 8. PAUSAR não para o collector.
# 9. Proteção contra repetição da mesma rodada-base.
# 10. As seis regras V8.5 são exclusivas e sem motor paralelo.
# 11. A previsão da regra é invertida antes da entrada real.
# 12. MG3: máximo de 3 tentativas, mesmo valor em todas, sem dobrar.
# ================================================================


import os
import threading
import time

import psycopg2
from flask import Flask, jsonify, render_template_string, redirect


app = Flask(__name__)


# ================================================================
# BANCO DE DADOS
# ================================================================

def get_db_connection():

    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        return None

    try:

        if "sslmode=" not in database_url:

            separator = "&" if "?" in database_url else "?"

            database_url = (
                f"{database_url}{separator}sslmode=require"
            )

        return psycopg2.connect(
            database_url,
            connect_timeout=10
        )

    except Exception as e:

        print(
            f"❌ Erro Neon: {e}",
            flush=True
        )

        return None


# ================================================================
# INICIALIZAÇÃO DO BANCO
# ================================================================

def init_web_db():

    conn = get_db_connection()

    if not conn:
        return

    try:

        with conn.cursor() as cur:

            # ----------------------------------------------------
            # HISTÓRICO DE RODADAS
            # ----------------------------------------------------

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

                    coletado_em TIMESTAMP
                        DEFAULT CURRENT_TIMESTAMP
                );
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_blaze_created_at
                ON blaze_historico(created_at);
            """)

            # ----------------------------------------------------
            # STATUS DO COLLECTOR
            # ----------------------------------------------------

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

                    atualizado_em TIMESTAMP
                        DEFAULT CURRENT_TIMESTAMP
                );
            """)

            cur.execute("""
                INSERT INTO collector_status (id)
                VALUES (1)

                ON CONFLICT (id)
                DO NOTHING;
            """)

            # ----------------------------------------------------
            # ESTADO DO BOT
            # ----------------------------------------------------

            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_estado (

                    id INTEGER PRIMARY KEY,

                    motor_ativo BOOLEAN DEFAULT FALSE,

                    sinal_ativo VARCHAR(150),

                    cor_sinal VARCHAR(5),

                    ultima_estrategia VARCHAR(100),

                    wins INTEGER DEFAULT 0,

                    losses INTEGER DEFAULT 0,

                    whites INTEGER DEFAULT 0,

                    profit FLOAT DEFAULT 0.0,

                    atualizado_em TIMESTAMP
                        DEFAULT CURRENT_TIMESTAMP,

                    inicio_sessao TIMESTAMP,

                    ultima_rodada_sinal VARCHAR(100),

                    sinal_base_id INTEGER,

                    ciclo_ativo BOOLEAN DEFAULT FALSE,

                    ciclo_id VARCHAR(120),

                    tentativa_atual INTEGER DEFAULT 0,

                    ciclo_cor_regra VARCHAR(5),

                    ciclo_cor_entrada VARCHAR(5),

                    ciclo_estrategia VARCHAR(150)
                );
            """)

            # ----------------------------------------------------
            # MIGRAÇÃO
            # ----------------------------------------------------

            cur.execute("""
                ALTER TABLE bot_estado
                ADD COLUMN IF NOT EXISTS inicio_sessao TIMESTAMP;
            """)

            cur.execute("""
                ALTER TABLE bot_estado
                ADD COLUMN IF NOT EXISTS ultima_rodada_sinal VARCHAR(100);
            """)

            cur.execute("""
                ALTER TABLE bot_estado
                ADD COLUMN IF NOT EXISTS sinal_base_id INTEGER;
            """)

            cur.execute("""
                ALTER TABLE bot_estado
                ADD COLUMN IF NOT EXISTS ciclo_ativo BOOLEAN DEFAULT FALSE;
            """)

            cur.execute("""
                ALTER TABLE bot_estado
                ADD COLUMN IF NOT EXISTS ciclo_id VARCHAR(120);
            """)

            cur.execute("""
                ALTER TABLE bot_estado
                ADD COLUMN IF NOT EXISTS tentativa_atual INTEGER DEFAULT 0;
            """)

            cur.execute("""
                ALTER TABLE bot_estado
                ADD COLUMN IF NOT EXISTS ciclo_cor_regra VARCHAR(5);
            """)

            cur.execute("""
                ALTER TABLE bot_estado
                ADD COLUMN IF NOT EXISTS ciclo_cor_entrada VARCHAR(5);
            """)

            cur.execute("""
                ALTER TABLE bot_estado
                ADD COLUMN IF NOT EXISTS ciclo_estrategia VARCHAR(150);
            """)

            # Compatibilidade com versões que criaram sinal_ativo como BOOLEAN.
            cur.execute("""
                DO $$
                BEGIN
                    IF EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name='bot_estado'
                          AND column_name='sinal_ativo'
                          AND data_type='boolean'
                    ) THEN
                        ALTER TABLE bot_estado
                        ALTER COLUMN sinal_ativo TYPE VARCHAR(150)
                        USING CASE WHEN sinal_ativo THEN 'ATIVO' ELSE NULL END;
                    END IF;
                END $$;
            """)

            cur.execute("""
                INSERT INTO bot_estado (
                    id,
                    motor_ativo
                )
                VALUES (
                    1,
                    FALSE
                )

                ON CONFLICT (id)
                DO NOTHING;
            """)

            # ----------------------------------------------------
            # HISTÓRICO DOS SINAIS
            # ----------------------------------------------------

            cur.execute("""
                CREATE TABLE IF NOT EXISTS estrategia_sinais (

                    id SERIAL PRIMARY KEY,

                    estrategia VARCHAR(100),

                    cor_prevista VARCHAR(5),

                    rodada_base VARCHAR(100),

                    rodada_resultado VARCHAR(100),

                    cor_resultado VARCHAR(20),

                    resultado VARCHAR(20),

                    criado_em TIMESTAMP
                        DEFAULT CURRENT_TIMESTAMP,

                    tentativa INTEGER DEFAULT 1,

                    ciclo_id VARCHAR(120),

                    cor_regra VARCHAR(5),

                    cor_entrada VARCHAR(5),

                    valor_aposta NUMERIC(12,2) DEFAULT 1.00
                );
            """)

            for name, definition in [
                ("tentativa", "INTEGER DEFAULT 1"),
                ("ciclo_id", "VARCHAR(120)"),
                ("cor_regra", "VARCHAR(5)"),
                ("cor_entrada", "VARCHAR(5)"),
                ("valor_aposta", "NUMERIC(12,2) DEFAULT 1.00"),
            ]:
                cur.execute(
                    f"ALTER TABLE estrategia_sinais ADD COLUMN IF NOT EXISTS {name} {definition};"
                )

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_estrategia_sinais_ciclo
                ON estrategia_sinais(ciclo_id);
            """)

            # ----------------------------------------------------
            # ÍNDICES
            # ----------------------------------------------------

            cur.execute("""
                CREATE INDEX IF NOT EXISTS
                idx_estrategia_sinais_criado_em
                ON estrategia_sinais(criado_em);
            """)

        conn.commit()

        conn.close()

        print(
            "✅ Banco inicializado.",
            flush=True
        )

    except Exception as e:

        print(
            f"❌ Erro init web DB: {e}",
            flush=True
        )

        try:
            conn.rollback()
            conn.close()
        except:
            pass


# ================================================================
# NORMALIZAÇÃO DAS CORES
#
# PADRÃO INTERNO:
#
# R = VERMELHO
# P = PRETO
# W = BRANCO
# ================================================================

def mapear_cor_letra(cor_str):

    if not cor_str:
        return None

    cor = str(cor_str).upper().strip()

    # VERMELHO
    if (
        "VERMELHO" in cor
        or cor == "RED"
        or cor == "R"
        or cor == "V"
        or cor == "VI"
    ):
        return "R"

    # PRETO
    if (
        "PRETO" in cor
        or cor == "BLACK"
        or cor == "P"
        or cor == "B"
    ):
        return "P"

    # BRANCO
    if (
        "BRANCO" in cor
        or cor == "WHITE"
        or cor == "W"
    ):
        return "W"

    return None


# ================================================================
# NORMALIZAÇÃO PELO CÓDIGO NUMÉRICO
#
# 0 = BRANCO
# 1 = VERMELHO
# 2 = PRETO
# ================================================================

def mapear_cor_numerica(color):

    try:

        color = int(color)

    except:

        return None

    if color == 0:
        return "W"

    if color == 1:
        return "R"

    if color == 2:
        return "P"

    return None


# ================================================================
# OBTÉM COR NORMALIZADA
# ================================================================

def obter_cor(cor_texto, color):

    cor = mapear_cor_letra(cor_texto)

    if cor:
        return cor

    return mapear_cor_numerica(color)


# ================================================================
# V8.5 — ESTRATÉGIAS
#
# A lógica das seis regras fica exclusivamente em strategy_engine.py.
# O app.py não possui um segundo motor de padrões, evitando conflito
# ou geração duplicada de sinais.
# ================================================================

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

    mapa = {
        "V8.5 R1 | R-W-P-R -> P": "R1 • R-W-P-R → P",
        "V8.5 R2 | P-W-R-P -> R": "R2 • P-W-R-P → R",
        "V8.5 R3 | R-W-R-P -> P": "R3 • R-W-R-P → P",
        "V8.5 R4 | P-W-P-P -> P": "R4 • P-W-P-P → P",
        "V8.5 R5 | P-W-R-R -> P": "R5 • P-W-R-R → P",
        "V8.5 R6 | R-W-R-R -> R": "R6 • R-W-R-R → R",
    }
    return mapa.get(nome, nome)


# ================================================================
# DASHBOARD — SEM CONTAMINAR A SESSÃO COM HISTÓRICO ANTIGO
# ================================================================

def consultar_dashboard():
    vazio = {
        "conectado": False,
        "ultima_rodada": None,
        "ultimo_resultado_em": None,
        "total_jogos": 0,
        "jogos": [],
        "motor": {
            "ativo": False,
            "sinal": False,
            "cor": None,
            "estrategia": None,
            "wins": 0,
            "losses": 0,
            "pendentes": 0,
            "profit": 0.0,
            "ciclo_ativo": False,
            "tentativa": 0,
            "cor_regra": None,
            "cor_entrada": None,
        },
        "historico_sinais": [],
    }

    conn = get_db_connection()
    if not conn:
        return vazio

    try:
        with conn.cursor() as cur:
            # --------------------------------------------------------
            # STATUS DO COLLECTOR
            # --------------------------------------------------------
            cur.execute("""
                SELECT conectado, ultima_rodada, ultimo_resultado_em
                FROM collector_status
                WHERE id = 1;
            """)
            row = cur.fetchone()
            if row:
                vazio["conectado"] = bool(row[0])
                vazio["ultima_rodada"] = row[1]
                vazio["ultimo_resultado_em"] = row[2]

            # --------------------------------------------------------
            # ESTADO DO MOTOR + INÍCIO DA SESSÃO
            # --------------------------------------------------------
            cur.execute("""
                SELECT motor_ativo, sinal_ativo, cor_sinal,
                       ultima_estrategia, inicio_sessao, ciclo_ativo,
                       tentativa_atual, ciclo_cor_regra, ciclo_cor_entrada, ciclo_estrategia
                FROM bot_estado
                WHERE id = 1;
            """)
            estado = cur.fetchone()

            inicio_sessao = estado[4] if estado else None
            motor_ativo = bool(estado[0]) if estado else False
            ciclo_ativo = bool(estado[5]) if estado else False
            tentativa_atual = int(estado[6] or 0) if estado else 0
            ciclo_cor_regra = estado[7] if estado else None
            ciclo_cor_entrada = estado[8] if estado else None
            ciclo_estrategia = estado[9] if estado else None

            # --------------------------------------------------------
            # PLACAR DA SESSÃO
            #
            # WHITE já é LOSS em estrategia_sinais. Portanto só existem
            # WIN, LOSS e PENDENTE no placar operacional.
            # --------------------------------------------------------
            if inicio_sessao:
                cur.execute("""
                    SELECT
                        COUNT(*) FILTER (WHERE resultado = 'WIN') AS wins,
                        COUNT(*) FILTER (WHERE resultado = 'LOSS') AS losses,
                        COUNT(*) FILTER (WHERE resultado = 'PENDENTE') AS pendentes,
                        COUNT(*) AS total
                    FROM estrategia_sinais
                    WHERE criado_em >= %s;
                """, (inicio_sessao,))
            else:
                cur.execute("""
                    SELECT
                        0 AS wins, 0 AS losses, 0 AS pendentes, 0 AS total;
                """)

            wins, losses, pendentes, _total = cur.fetchone()
            wins = int(wins or 0)
            losses = int(losses or 0)
            pendentes = int(pendentes or 0)
            profit = (wins - losses) * 1.0

            # --------------------------------------------------------
            # SINAL ATIVO — SOMENTE DA SESSÃO ATUAL
            # --------------------------------------------------------
            sinal = None
            if inicio_sessao and motor_ativo and ciclo_ativo:
                cur.execute("""
                    SELECT estrategia, cor_prevista, cor_regra, cor_entrada,
                           tentativa, valor_aposta
                    FROM estrategia_sinais
                    WHERE criado_em >= %s
                      AND resultado = 'PENDENTE'
                    ORDER BY id DESC
                    LIMIT 1;
                """, (inicio_sessao,))
                sinal = cur.fetchone()

            if sinal:
                estrategia_ativa, cor_sinal, cor_regra_sinal, cor_entrada_sinal, tentativa_sinal, valor_sinal = sinal
                vazio["motor"] = {
                    "ativo": motor_ativo,
                    "sinal": True,
                    "cor": cor_sinal,
                    "cor_regra": cor_regra_sinal or ciclo_cor_regra,
                    "cor_entrada": cor_entrada_sinal or ciclo_cor_entrada,
                    "tentativa": int(tentativa_sinal or tentativa_atual),
                    "valor_aposta": float(valor_sinal or 1.0),
                    "ciclo_ativo": True,
                    "estrategia": estrategia_curta(estrategia_ativa),
                    "wins": wins,
                    "losses": losses,
                    "pendentes": pendentes,
                    "profit": profit,
                }
            else:
                vazio["motor"] = {
                    "ativo": motor_ativo,
                    "sinal": False,
                    "cor": ciclo_cor_entrada if ciclo_ativo else None,
                    "cor_regra": ciclo_cor_regra if ciclo_ativo else None,
                    "cor_entrada": ciclo_cor_entrada if ciclo_ativo else None,
                    "tentativa": tentativa_atual if ciclo_ativo else 0,
                    "valor_aposta": 1.0,
                    "ciclo_ativo": ciclo_ativo,
                    "estrategia": estrategia_curta(ciclo_estrategia) if ciclo_ativo else None,
                    "wins": wins,
                    "losses": losses,
                    "pendentes": pendentes,
                    "profit": profit,
                }

            # --------------------------------------------------------
            # RODADAS DA SESSÃO
            # --------------------------------------------------------
            if inicio_sessao:
                cur.execute("""
                    SELECT roll, color, cor, rodada_id
                    FROM blaze_historico
                    WHERE status = 'complete'
                      AND coletado_em >= %s
                    ORDER BY id DESC
                    LIMIT 24;
                """, (inicio_sessao,))
                rows = list(reversed(cur.fetchall()))
            else:
                rows = []

            vazio["total_jogos"] = len(rows)
            for roll, color, cor, rodada_id in rows:
                vazio["jogos"].append({
                    "roll": roll,
                    "color": color,
                    "cor": cor,
                    "rodada": rodada_id,
                    **cor_info(color, cor),
                })

            # --------------------------------------------------------
            # SINAIS DA SESSÃO — NUNCA DO HISTÓRICO ANTERIOR
            # --------------------------------------------------------
            if inicio_sessao:
                cur.execute("""
                    SELECT estrategia, cor_prevista, rodada_base,
                           rodada_resultado, cor_resultado, resultado, criado_em,
                           tentativa, ciclo_id, cor_regra, cor_entrada, valor_aposta
                    FROM estrategia_sinais
                    WHERE criado_em >= %s
                    ORDER BY id DESC
                    LIMIT 12;
                """, (inicio_sessao,))

                for (
                    estrategia,
                    prevista,
                    base,
                    rodada_resultado,
                    cor_resultado,
                    resultado,
                    criado,
                    tentativa,
                    ciclo_id,
                    cor_regra,
                    cor_entrada,
                    valor_aposta,
                ) in cur.fetchall():
                    # Compatibilidade com registros antigos que ainda possam
                    # ter WHITE escrito no campo resultado.
                    if resultado == "WHITE":
                        resultado = "LOSS"

                    vazio["historico_sinais"].append({
                        "estrategia": estrategia_curta(estrategia),
                        "estrategia_full": estrategia,
                        "prevista": prevista,
                        "base": base,
                        "rodada_resultado": rodada_resultado,
                        "cor_resultado": cor_resultado,
                        "resultado": resultado,
                        "criado_em": criado,
                        "tentativa": int(tentativa or 1),
                        "ciclo_id": ciclo_id,
                        "cor_regra": cor_regra,
                        "cor_entrada": cor_entrada or prevista,
                        "valor_aposta": float(valor_aposta or 1.0),
                    })

        conn.close()
        return vazio

    except Exception as e:
        print(f"❌ Erro dashboard: {e}", flush=True)
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass
        return vazio


HTML = r"""
<!DOCTYPE html>

<html lang="pt-br">

<head>

<meta charset="UTF-8">

<meta
    name="viewport"
    content="width=device-width, initial-scale=1.0"
>

<meta
    http-equiv="refresh"
    content="5"
>

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
            #1a202b 0%,
            var(--bg) 48%
        );

    color: var(--text);

    font-family:
        Inter,
        Arial,
        sans-serif;

    padding: 20px;

}

.container {

    max-width: 1180px;

    margin: 0 auto;

}

.header {

    display: flex;

    justify-content: space-between;

    align-items: center;

    margin-bottom: 16px;

    gap: 15px;

    flex-wrap: wrap;

}

.logo {

    font-size: 25px;

    font-weight: 900;

}

.logo span {

    color: var(--red);

}

.header-right {

    display: flex;

    align-items: center;

    gap: 15px;

}

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

    box-shadow:
        0 0 12px currentColor;

}

.top-grid {

    display: grid;

    grid-template-columns:
        1.2fr
        1fr
        1fr
        1fr;

    gap: 10px;

    margin-bottom: 12px;

}

.card {

    background:
        rgba(16,20,27,.92);

    border:
        1px solid var(--border);

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

.big {

    font-size: 23px;

    font-weight: 950;

}

.small {

    color: var(--muted);

    font-size: 11px;

    margin-top: 5px;

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

.black {

    color: #d7dce3;

}

.signal {

    margin-bottom: 12px;

    padding: 24px;

    border-radius: 18px;

    background:
        linear-gradient(
            145deg,
            rgba(22,27,36,.98),
            rgba(12,15,21,.98)
        );

    border:
        1px solid rgba(255,255,255,.10);

    text-align: center;

}

.signal.active {

    border-color:
        rgba(35,226,124,.28);

    box-shadow:
        0 0 35px
        rgba(35,226,124,.06);

}

.signal .eyebrow {

    color: var(--muted);

    font-size: 11px;

    font-weight: 900;

    text-transform: uppercase;

    letter-spacing: 1px;

}

.signal .color {

    font-size: 38px;

    font-weight: 950;

    margin: 7px 0 4px;

}

.signal .strategy {

    font-size: 15px;

    font-weight: 850;

}

.signal .entry {

    margin-top: 9px;

    color: var(--muted);

    font-size: 12px;

}

.section-title {

    display: flex;

    justify-content: space-between;

    align-items: end;

    margin: 18px 0 9px;

}

.section-title h2 {

    font-size: 13px;

    text-transform: uppercase;

    letter-spacing: .8px;

}

.section-title span {

    color: var(--muted);

    font-size: 11px;

}

.performance {

    display: grid;

    grid-template-columns:
        repeat(4, 1fr);

    gap: 9px;

    margin-bottom: 12px;

}

.metric {

    text-align: center;

    padding: 13px 8px;

    background:
        rgba(16,20,27,.92);

    border:
        1px solid var(--border);

    border-radius: 13px;

}

.metric .num {

    font-size: 22px;

    font-weight: 950;

}

.metric .m-label {

    margin-top: 4px;

    color: var(--muted);

    font-size: 9px;

    text-transform: uppercase;

    font-weight: 900;

}

.history {

    background:
        rgba(16,20,27,.92);

    border:
        1px solid var(--border);

    border-radius: 15px;

    overflow: hidden;

}

.history-row {

    display: grid;

    grid-template-columns:
        1.7fr
        .7fr
        1fr
        1fr
        1fr;

    gap: 8px;

    align-items: center;

    padding: 11px 13px;

    border-bottom:
        1px solid
        rgba(255,255,255,.045);

    font-size: 11px;

}

.history-row:last-child {

    border-bottom: 0;

}

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

.result-win {

    background:
        rgba(35,226,124,.12);

    color: var(--green);

}

.result-loss {

    background:
        rgba(255,77,93,.12);

    color: var(--red);

}

.results {

    display: flex;

    gap: 7px;

    overflow-x: auto;

    padding: 12px;

    background:
        rgba(16,20,27,.92);

    border:
        1px solid var(--border);

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

.pill small {

    font-size: 8px;

    opacity: .75;

    margin-top: 2px;

}

.pill.red-bg {

    background: var(--red);

    color: white;

}

.pill.black-bg {

    background: #262c35;

    color: white;

    border:
        1px solid #444b55;

}

.pill.white-bg {

    background: white;

    color: #111;

}

.footer {

    text-align: center;

    color: #56606d;

    font-size: 9px;

    padding: 18px 0 4px;

}

.control-btn {

    border: none;

    padding: 12px 24px;

    border-radius: 12px;

    font-weight: 900;

    cursor: pointer;

    font-size: 14px;

    color: white;

}

.control-btn.start {

    background: var(--green);

}

.control-btn.pause {

    background: var(--red);

}

.empty {

    padding: 25px;

    text-align: center;

    color: var(--muted);

    font-size: 12px;

}

@media (max-width: 900px) {

    .top-grid {

        grid-template-columns:
            repeat(2,1fr);

    }

    .performance {

        grid-template-columns:
            repeat(2,1fr);

    }

}

@media (max-width: 600px) {

    body {

        padding: 12px;

    }

    .top-grid {

        grid-template-columns: 1fr;

    }

    .performance {

        grid-template-columns:
            repeat(2,1fr);

    }

    .history-row {

        grid-template-columns:
            1.4fr
            .7fr
            1fr
            1fr;

    }

    .hide-mobile {

        display: none;

    }

}

</style>

</head>

<body>

<div class="container">

    <div class="header">

        <div class="logo">

            🤖 Blaze
            <span>Bot</span>

        </div>

        <div class="header-right">

            <form
                action="/controle_motor"
                method="POST"
            >

                {% if status.motor.ativo %}

                    <button
                        type="submit"
                        class="control-btn pause"
                    >
                        ⏸️ PAUSAR
                    </button>

                {% else %}

                    <button
                        type="submit"
                        class="control-btn start"
                    >
                        ▶️ INICIAR
                    </button>

                {% endif %}

            </form>

            <div class="live">

                <span class="dot"></span>

                {% if status.conectado %}

                    COLETOR ONLINE

                {% else %}

                    COLETOR OFFLINE

                {% endif %}

            </div>

        </div>

    </div>


    <div class="top-grid">

        <div class="card">

            <div class="label">
                Última rodada
            </div>

            <div class="big">

                {{ status.ultima_rodada or 'Aguardando...' }}

            </div>

            <div class="small">

                Atualização automática a cada 5s

            </div>

        </div>


        <div class="card">

            <div class="label">
                Rodadas da sessão
            </div>

            <div class="big yellow">

                {{ status.total_jogos }}

            </div>

            <div class="small">

                Desde o último INICIAR

            </div>

        </div>


        <div class="card">

            <div class="label">
                Motor
            </div>

            {% if status.motor.ativo %}

                <div class="big green">
                    🟢 ATIVO
                </div>

            {% else %}

                <div class="big red">
                    🔴 PAUSADO
                </div>

            {% endif %}

            <div class="small">

                Coleta continua mesmo pausado

            </div>

        </div>


        <div class="card">

            <div class="label">
                Placar
            </div>

            <div class="big">

                {{ status.motor.wins }}W /
                {{ status.motor.losses }}L

            </div>

            <div class="small">

                WHITE = LOSS

            </div>

        </div>

    </div>


    {% if status.motor.ativo and status.motor.sinal %}

        <div class="signal active">

            <div class="eyebrow">

                🎯 SINAL ATUAL
                • PRÓXIMA RODADA

            </div>


            {% if status.motor.cor == 'R' %}

                <div class="color red">

                    🔴 VERMELHO

                </div>

            {% elif status.motor.cor == 'P' %}

                <div class="color black">

                    ⚫ PRETO

                </div>

            {% endif %}


            <div class="strategy">

                {{ status.motor.estrategia or 'Sinal ativo' }}

            </div>

            <div class="entry">

                Tentativa {{ status.motor.tentativa }}/3
                • Valor fixo: R$ {{ '%.2f'|format(status.motor.valor_aposta or 1.0) }}
                • Sem dobrar

            </div>


            <div class="entry">

                A entrada usa a cor invertida da regra.
                Se perder, tenta novamente até 3 vezes,
                sempre com o mesmo valor.

            </div>

        </div>

    {% else %}

        <div class="signal">

            <div class="eyebrow">

                🎯 SINAL ATUAL

            </div>

            <div
                class="color"
                style="font-size:27px"
            >

                ⚪ AGUARDANDO GATILHO

            </div>

            <div class="strategy">

                {% if status.motor.ativo %}

                    Nenhum padrão encontrado.

                {% else %}

                    Motor pausado.

                {% endif %}

            </div>

            <div class="entry">

                A coleta permanece ativa
                independentemente do motor.

            </div>

        </div>

    {% endif %}


    <div class="section-title">

        <h2>
            📊 Desempenho
        </h2>

        <span>
            Sessão atual
        </span>

    </div>


    <div class="performance">

        <div class="metric">

            <div class="num green">

                {{ status.motor.wins }}

            </div>

            <div class="m-label">
                Acertos
            </div>

        </div>


        <div class="metric">

            <div class="num red">

                {{ status.motor.losses }}

            </div>

            <div class="m-label">
                Erros
            </div>

        </div>


        <div class="metric">

            <div class="num">

                {{
                    status.motor.wins
                    +
                    status.motor.losses
                }}

            </div>

            <div class="m-label">
                Sinais resolvidos
            </div>

        </div>


        <div class="metric">

            <div class="num yellow">

                {% set total_res =
                    status.motor.wins
                    +
                    status.motor.losses
                %}

                {% if total_res > 0 %}

                    {{
                        '%.1f'|format(
                            status.motor.wins
                            /
                            total_res
                            *
                            100
                        )
                    }}%

                {% else %}

                    —

                {% endif %}

            </div>

            <div class="m-label">
                Taxa de acerto
            </div>

        </div>

    </div>


    <div class="section-title">

        <h2>
            🧾 Últimos sinais
        </h2>

        <span>
            Sessão atual
        </span>

    </div>


    <div class="history">

        <div class="history-row history-head">

            <div>
                Estratégia
            </div>

            <div>
                Entrada
            </div>

            <div>
                Rodada base
            </div>

            <div>
                Resultado
            </div>

            <div class="hide-mobile">
                Rodada resolvida
            </div>

        </div>


        {% if status.historico_sinais %}

            {% for s in status.historico_sinais %}

                <div class="history-row">

                    <div>

                        {{ s.estrategia }}

                    </div>


                    <div>

                        {% if s.cor_entrada == 'R' %}

                            <span class="red">
                                🔴 R
                            </span>
                        {% elif s.cor_entrada == 'P' %}
                            <span class="black">
                                ⚫ P
                            </span>
                        {% endif %}

                        <div style="font-size:11px;color:#8993a1;margin-top:4px;">
                            {% if s.cor_regra %}
                                Regra: {{ s.cor_regra }} → Entrada: {{ s.cor_entrada }}
                            {% endif %}
                            • Tent. {{ s.tentativa }}/3
                        </div>

                    </div>


                    <div>

                        {{ s.base }}

                    </div>


                    <div>

                        {% if s.resultado == 'WIN' %}

                            <span
                                class="result-badge result-win"
                            >
                                ✓ WIN
                            </span>

                        {% elif s.resultado == 'LOSS' %}

                            <span
                                class="result-badge result-loss"
                            >
                                ✕ LOSS
                            </span>

                        {% endif %}

                    </div>


                    <div class="hide-mobile">

                        {{ s.rodada_resultado or '—' }}

                    </div>

                </div>

            {% endfor %}

        {% else %}

            <div class="empty">

                Ainda não há sinais registrados
                nesta sessão.

            </div>

        {% endif %}

    </div>


    <div class="section-title">

        <h2>
            🎲 Últimos resultados
        </h2>

        <span>
            Rodadas da sessão
        </span>

    </div>


    <div class="results">

        {% if status.jogos %}

            {% for jogo in status.jogos %}

                <div
                    class="
                        pill

                        {% if jogo.sigla == 'R' %}
                            red-bg

                        {% elif jogo.sigla == 'P' %}
                            black-bg

                        {% else %}
                            white-bg

                        {% endif %}
                    "

                    title="{{ jogo.rodada }}"
                >

                    {{ jogo.emoji }}
                    {{ jogo.roll }}

                    <small>
                        {{ jogo.sigla }}
                    </small>

                </div>

            {% endfor %}

        {% else %}

            <div class="empty">

                Aguardando novas rodadas...

            </div>

        {% endif %}

    </div>


    <div class="footer">

        Blaze Bot V8.5 • coleta contínua •
        seis regras com branco •
        WHITE = LOSS •
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
    conn = get_db_connection()
    if not conn:
        return redirect("/")

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT motor_ativo FROM bot_estado WHERE id = 1;")
            estado = cur.fetchone()
            if not estado:
                conn.close()
                return redirect("/")

            motor_ativo = bool(estado[0])

            if motor_ativo:
                print("⏸️ Motor pausado pelo dashboard. Collector continua.", flush=True)
                cur.execute("""
                    UPDATE bot_estado
                    SET motor_ativo = FALSE,
                        atualizado_em = CURRENT_TIMESTAMP
                    WHERE id = 1;
                """)
            else:
                print("▶️ Iniciando NOVA SESSÃO estatística.", flush=True)
                cur.execute("""
                    UPDATE bot_estado
                    SET motor_ativo = TRUE,
                        inicio_sessao = CURRENT_TIMESTAMP,
                        ciclo_ativo = FALSE,
                        ciclo_id = NULL,
                        tentativa_atual = 0,
                        ciclo_cor_regra = NULL,
                        ciclo_cor_entrada = NULL,
                        ciclo_estrategia = NULL,
                        sinal_ativo = NULL,
                        cor_sinal = NULL,
                        ultima_estrategia = NULL,
                        rodada_base_sinal = NULL,
                        atualizado_em = CURRENT_TIMESTAMP
                    WHERE id = 1;
                """)

        conn.commit()
    except Exception as e:
        print(f"❌ Erro ao alterar estado do motor: {e}", flush=True)
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass

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
