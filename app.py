# ================================================================
# BLAZE BOT — DASHBOARD WEB + COLLECTOR + MOTOR ESTATÍSTICO
#
# VERSÃO: V8.0 GOLDEN PATTERNS — DUAS REGRAS + ENTRADA DIRETA (FLAT BETTING)
#
# Estratégias ativas, definidas em strategy_engine.py:
#   1) VERMELHO -> BRANCO -> PRETO -> VERMELHO -> PRETO
#   4) PRETO -> BRANCO -> PRETO -> PRETO -> PRETO
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
# 10. Apenas as Regras 1 e 4 estão ativas (validadas estatisticamente).
# 11. A previsão da regra é usada diretamente (SEM inversão).
# 12. FLAT BETTING: 1 tentativa, sem dobrar (sem Martingale).
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
                DO $$                 BEGIN
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
# V8.0 — ESTRATÉGIAS
#
# A lógica das regras fica exclusivamente em strategy_engine.py.
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
        "V8.5 R4 | P-W-P-P -> P": "R4 • P-W-P-P → P",
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

    background
