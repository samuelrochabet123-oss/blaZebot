# ================================================================
# BLAZE BOT — DASHBOARD WEB + COLLECTOR + MOTOR ESTATÍSTICO
# VERSÃO CORRIGIDA
#
# CORREÇÕES:
# 1. WIN/LOSS agora usa R = VERMELHO / P = PRETO / W = BRANCO
# 2. Impede repetição da mesma entrada
# 3. Cada sinal é resolvido SOMENTE na próxima rodada após a base
# 4. Ao INICIAR, começa uma nova sessão
# 5. Histórico de rodadas é zerado visualmente por sessão
# 6. Histórico de sinais é zerado visualmente por sessão
# 7. Placar WIN/LOSS/WHITE/PROFIT é zerado ao iniciar
# 8. Removido painel individual das estratégias
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
            database_url = f"{database_url}{separator}sslmode=require"

        return psycopg2.connect(
            database_url,
            connect_timeout=10
        )

    except Exception as e:
        print(f"❌ Erro Neon: {e}", flush=True)
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
                    coletado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_blaze_created_at
                ON blaze_historico(created_at);
            """)

            # ----------------------------------------------------
            # STATUS DO COLETOR
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
                    atualizado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            cur.execute("""
                INSERT INTO collector_status (id)
                VALUES (1)
                ON CONFLICT (id) DO NOTHING;
            """)

            # ----------------------------------------------------
            # ESTADO DO BOT
            # ----------------------------------------------------

            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_estado (
                    id INTEGER PRIMARY KEY,

                    motor_ativo BOOLEAN DEFAULT FALSE,
                    sinal_ativo BOOLEAN DEFAULT FALSE,

                    cor_sinal VARCHAR(5),
                    ultima_estrategia VARCHAR(100),

                    wins INTEGER DEFAULT 0,
                    losses INTEGER DEFAULT 0,
                    whites INTEGER DEFAULT 0,

                    profit FLOAT DEFAULT 0.0,

                    atualizado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

                    -- Controle da sessão atual
                    inicio_sessao TIMESTAMP,

                    -- Última rodada utilizada como base de sinal
                    ultima_rodada_sinal VARCHAR(100),

                    -- ID interno da rodada que gerou o sinal
                    sinal_base_id INTEGER
                );
            """)

            # ----------------------------------------------------
            # MIGRAÇÃO PARA BANCO ANTIGO
            # ----------------------------------------------------
            # Caso a tabela bot_estado já exista da versão anterior,
            # estas colunas serão adicionadas automaticamente.

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
                INSERT INTO bot_estado (
                    id,
                    motor_ativo
                )
                VALUES (
                    1,
                    FALSE
                )
                ON CONFLICT (id) DO NOTHING;
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

                    criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

        conn.commit()
        conn.close()

        print("✅ Banco inicializado.", flush=True)

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
# ================================================================
#
# IMPORTANTE:
#
# R = VERMELHO
# P = PRETO
# W = BRANCO
#
# Antes o código misturava:
# V / P / B
# com
# R / B
#
# Isso fazia vários WINs serem contabilizados como LOSS.
# ================================================================

def mapear_cor_letra(cor_str):

    if not cor_str:
        return None

    cor = str(cor_str).upper().strip()

    # VERMELHO
    if "VERMELHO" in cor or cor == "RED":
        return "R"

    # PRETO
    if "PRETO" in cor or cor == "BLACK":
        return "P"

    # BRANCO
    if "BRANCO" in cor or cor == "WHITE":
        return "W"

    return None


# ================================================================
# MOTOR DE PADRÕES
# ================================================================

def motor_de_padroes():

    print(
        "🧠 Motor de Padrões Estatísticos iniciado em background...",
        flush=True
    )

    while True:

        conn = get_db_connection()

        if not conn:
            time.sleep(5)
            continue

        try:

            with conn.cursor() as cur:

                # ------------------------------------------------
                # VERIFICA SE O MOTOR ESTÁ ATIVO
                # ------------------------------------------------

                cur.execute("""
                    SELECT
                        motor_ativo,
                        sinal_ativo,
                        cor_sinal,
                        ultima_estrategia,
                        inicio_sessao,
                        ultima_rodada_sinal,
                        sinal_base_id
                    FROM bot_estado
                    WHERE id = 1;
                """)

                estado = cur.fetchone()

                if not estado:

                    conn.close()
                    time.sleep(5)
                    continue

                (
                    motor_ativo,
                    sinal_ativo,
                    cor_sinal,
                    ultima_estrategia,
                    inicio_sessao,
                    ultima_rodada_sinal,
                    sinal_base_id
                ) = estado

                # ------------------------------------------------
                # MOTOR PAUSADO
                # ------------------------------------------------

                if not motor_ativo:

                    conn.close()
                    time.sleep(5)
                    continue

                # =================================================
                # 1. EXISTE SINAL PENDENTE?
                # =================================================
                #
                # Aqui está uma das correções mais importantes.
                #
                # Não pegamos simplesmente a última rodada.
                #
                # Procuramos a PRIMEIRA rodada completa posterior
                # à rodada que gerou o sinal.
                #
                # Assim:
                #
                # rodada 100 = base
                # sinal = PRETO
                #
                # rodada 101 = resultado do sinal
                #
                # O bot não vai resolver novamente na 102, 103...
                # =================================================

                if sinal_ativo and sinal_base_id:

                    cur.execute("""
                        SELECT
                            id,
                            rodada_id,
                            cor,
                            color,
                            roll
                        FROM blaze_historico
                        WHERE
                            status = 'complete'
                            AND id > %s
                        ORDER BY id ASC
                        LIMIT 1;
                    """, (sinal_base_id,))

                    resultado = cur.fetchone()

                    # Ainda não chegou a próxima rodada
                    if not resultado:

                        conn.close()
                        time.sleep(3)
                        continue

                    (
                        resultado_id,
                        rodada_resultado,
                        cor_resultado,
                        color_resultado,
                        roll_resultado
                    ) = resultado

                    # ------------------------------------------------
                    # NORMALIZA COR REAL
                    # ------------------------------------------------

                    cor_real = mapear_cor_letra(cor_resultado)

                    # Se não conseguiu pela coluna cor,
                    # tenta pela coluna numérica.
                    if cor_real is None:

                        try:

                            numero_cor = int(color_resultado)

                            if numero_cor == 0:
                                cor_real = "W"

                            elif numero_cor == 1:
                                cor_real = "R"

                            elif numero_cor == 2:
                                cor_real = "P"

                        except:
                            cor_real = None

                    # ------------------------------------------------
                    # DETERMINA RESULTADO
                    # ------------------------------------------------

                    if cor_real == "W":

                        resultado_status = "WHITE"

                    elif cor_real == cor_sinal:

                        resultado_status = "WIN"

                    else:

                        resultado_status = "LOSS"

                    print(
                        f"🎯 SINAL RESOLVIDO | "
                        f"Previsão={cor_sinal} | "
                        f"Resultado={cor_real} | "
                        f"Rodada={rodada_resultado} | "
                        f"Status={resultado_status}",
                        flush=True
                    )

                    # ------------------------------------------------
                    # REGISTRA O SINAL
                    # ------------------------------------------------

                    cur.execute("""
                        INSERT INTO estrategia_sinais
                        (
                            estrategia,
                            cor_prevista,
                            rodada_base,
                            rodada_resultado,
                            cor_resultado,
                            resultado,
                            criado_em
                        )
                        VALUES
                        (
                            %s,
                            %s,
                            %s,
                            %s,
                            %s,
                            %s,
                            CURRENT_TIMESTAMP
                        );
                    """, (
                        ultima_estrategia,
                        cor_sinal,
                        ultima_rodada_sinal,
                        rodada_resultado,
                        cor_resultado,
                        resultado_status
                    ))

                    # ------------------------------------------------
                    # ATUALIZA PLACAR
                    # ------------------------------------------------

                    if resultado_status == "WIN":

                        cur.execute("""
                            UPDATE bot_estado
                            SET
                                wins = wins + 1,
                                profit = profit + 1.0,
                                sinal_ativo = FALSE,
                                cor_sinal = NULL,
                                ultima_rodada_sinal = NULL,
                                sinal_base_id = NULL,
                                atualizado_em = CURRENT_TIMESTAMP
                            WHERE id = 1;
                        """)

                    elif resultado_status == "LOSS":

                        cur.execute("""
                            UPDATE bot_estado
                            SET
                                losses = losses + 1,
                                profit = profit - 1.0,
                                sinal_ativo = FALSE,
                                cor_sinal = NULL,
                                ultima_rodada_sinal = NULL,
                                sinal_base_id = NULL,
                                atualizado_em = CURRENT_TIMESTAMP
                            WHERE id = 1;
                        """)

                    elif resultado_status == "WHITE":

                        # BRANCO NÃO É WIN.
                        # Também não é LOSS.
                        # Fica contabilizado separadamente.

                        cur.execute("""
                            UPDATE bot_estado
                            SET
                                whites = whites + 1,
                                sinal_ativo = FALSE,
                                cor_sinal = NULL,
                                ultima_rodada_sinal = NULL,
                                sinal_base_id = NULL,
                                atualizado_em = CURRENT_TIMESTAMP
                            WHERE id = 1;
                        """)

                    conn.commit()
                    conn.close()

                    time.sleep(2)
                    continue

                # =================================================
                # 2. NÃO EXISTE SINAL — PROCURAR NOVO GATILHO
                # =================================================

                cur.execute("""
                    SELECT
                        id,
                        rodada_id,
                        cor,
                        color,
                        roll
                    FROM blaze_historico
                    WHERE status = 'complete'
                    ORDER BY id DESC
                    LIMIT 3;
                """)

                rows = cur.fetchall()

                if len(rows) < 3:

                    conn.close()
                    time.sleep(3)
                    continue

                # Ordem cronológica
                hist = list(reversed(rows))

                rodada_anterior = hist[-2]
                rodada_atual = hist[-1]

                id_anterior = rodada_anterior[0]
                rodada_id_anterior = rodada_anterior[1]
                cor_anterior = rodada_anterior[2]
                color_anterior = rodada_anterior[3]

                id_atual = rodada_atual[0]
                rodada_id_atual = rodada_atual[1]
                cor_atual = rodada_atual[2]
                color_atual = rodada_atual[3]

                # ------------------------------------------------
                # NORMALIZA CORES
                # ------------------------------------------------

                cor1 = mapear_cor_letra(cor_anterior)
                cor2 = mapear_cor_letra(cor_atual)

                # Fallback para código numérico
                if cor1 is None:

                    try:

                        c = int(color_anterior)

                        if c == 0:
                            cor1 = "W"
                        elif c == 1:
                            cor1 = "R"
                        elif c == 2:
                            cor1 = "P"

                    except:
                        pass

                if cor2 is None:

                    try:

                        c = int(color_atual)

                        if c == 0:
                            cor2 = "W"
                        elif c == 1:
                            cor2 = "R"
                        elif c == 2:
                            cor2 = "P"

                    except:
                        pass

                if cor1 is None or cor2 is None:

                    conn.close()
                    time.sleep(3)
                    continue

                # =================================================
                # GATILHO
                # =================================================
                #
                # Estratégia atual:
                #
                # PRETO + PRETO
                #       ↓
                # VERMELHO
                #
                # O sinal é criado usando a rodada atual como BASE.
                #
                # Exemplo:
                #
                # 500 = PRETO
                # 501 = PRETO
                #       ↓
                # SINAL PARA VERMELHO
                #       ↓
                # 502 = resultado
                #
                # =================================================

                seq_2 = cor1 + cor2

                if seq_2 == "PP":

                    # ------------------------------------------------
                    # PROTEÇÃO CONTRA REPETIÇÃO
                    # ------------------------------------------------
                    #
                    # Se essa rodada já foi usada como base,
                    # NÃO cria outro sinal.
                    #
                    if ultima_rodada_sinal == rodada_id_atual:

                        conn.close()
                        time.sleep(3)
                        continue

                    print(
                        f"📊 NOVO SINAL | "
                        f"2x PRETO | "
                        f"Base={rodada_id_atual} | "
                        f"Entrada=VERMELHO",
                        flush=True
                    )

                    cur.execute("""
                        UPDATE bot_estado
                        SET
                            sinal_ativo = TRUE,
                            cor_sinal = 'R',
                            ultima_estrategia = 'EST DATA (2x Preto -> V)',
                            ultima_rodada_sinal = %s,
                            sinal_base_id = %s,
                            atualizado_em = CURRENT_TIMESTAMP
                        WHERE id = 1;
                    """, (
                        rodada_id_atual,
                        id_atual
                    ))

                    conn.commit()

                conn.close()

        except Exception as e:

            print(
                f"❌ Erro no Motor de Padrões: {e}",
                flush=True
            )

            try:
                conn.rollback()
                conn.close()
            except:
                pass

        time.sleep(3)


# ================================================================
# INFORMAÇÕES DAS CORES PARA O DASHBOARD
# ================================================================

def cor_info(color, cor_texto=None):

    try:
        color = int(color)

    except:
        color = None

    # 0 = BRANCO
    if color == 0:

        return {
            "sigla": "W",
            "nome": "BRANCO",
            "classe": "white",
            "emoji": "⚪"
        }

    # 1 = VERMELHO
    if color == 1:

        return {
            "sigla": "R",
            "nome": "VERMELHO",
            "classe": "red",
            "emoji": "🔴"
        }

    # 2 = PRETO
    if color == 2:

        return {
            "sigla": "P",
            "nome": "PRETO",
            "classe": "black",
            "emoji": "⚫"
        }

    texto = (cor_texto or "").upper()

    if "VERMELHO" in texto or texto == "RED":

        return {
            "sigla": "R",
            "nome": "VERMELHO",
            "classe": "red",
            "emoji": "🔴"
        }

    if "PRETO" in texto or texto == "BLACK":

        return {
            "sigla": "P",
            "nome": "PRETO",
            "classe": "black",
            "emoji": "⚫"
        }

    return {
        "sigla": "W",
        "nome": "BRANCO",
        "classe": "white",
        "emoji": "⚪"
    }


# ================================================================
# NOME CURTO DA ESTRATÉGIA
# ================================================================

def estrategia_curta(nome):

    if not nome:
        return "—"

    return (
        nome
        .replace(
            "EST DATA (2x Preto -> V)",
            "📊 EST DATA • 2P → V"
        )
    )


# ================================================================
# CONSULTAR DASHBOARD
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

            "whites": 0,

            "profit": 0.0

        },

        "historico_sinais": []
    }

    conn = get_db_connection()

    if not conn:
        return vazio

    try:

        with conn.cursor() as cur:

            # ----------------------------------------------------
            # STATUS COLETOR
            # ----------------------------------------------------

            cur.execute("""
                SELECT
                    conectado,
                    ultima_rodada,
                    ultimo_resultado_em
                FROM collector_status
                WHERE id = 1;
            """)

            row = cur.fetchone()

            if row:

                vazio["conectado"] = bool(row[0])
                vazio["ultima_rodada"] = row[1]
                vazio["ultimo_resultado_em"] = row[2]

            # ----------------------------------------------------
            # ESTADO DO BOT
            # ----------------------------------------------------

            cur.execute("""
                SELECT
                    motor_ativo,
                    sinal_ativo,
                    cor_sinal,
                    ultima_estrategia,
                    wins,
                    losses,
                    whites,
                    profit,
                    inicio_sessao
                FROM bot_estado
                WHERE id = 1;
            """)

            estado = cur.fetchone()

            inicio_sessao = None

            if estado:

                (
                    motor_ativo,
                    sinal_ativo,
                    cor_sinal,
                    ultima_estrategia,
                    wins,
                    losses,
                    whites,
                    profit,
                    inicio_sessao
                ) = estado

                vazio["motor"] = {

                    "ativo": bool(motor_ativo),

                    "sinal": bool(sinal_ativo),

                    "cor": cor_sinal,

                    "estrategia": (
                        estrategia_curta(ultima_estrategia)
                        if ultima_estrategia
                        else None
                    ),

                    "wins": wins or 0,

                    "losses": losses or 0,

                    "whites": whites or 0,

                    "profit": float(profit or 0)

                }

            # ----------------------------------------------------
            # HISTÓRICO DE RODADAS DA SESSÃO
            # ----------------------------------------------------
            #
            # Muito importante:
            #
            # Não apagamos o banco.
            #
            # Apenas mostramos as rodadas coletadas APÓS o
            # início da sessão atual.
            #
            # Portanto, ao clicar INICIAR:
            #
            # histórico visual = 0
            #
            # depois:
            #
            # nova rodada = 1
            # nova rodada = 2
            # nova rodada = 3
            #
            # ----------------------------------------------------

            if inicio_sessao:

                cur.execute("""
                    SELECT
                        roll,
                        color,
                        cor,
                        rodada_id
                    FROM blaze_historico
                    WHERE
                        status = 'complete'
                        AND coletado_em >= %s
                    ORDER BY id DESC
                    LIMIT 24;
                """, (inicio_sessao,))

            else:

                # Caso ainda não exista sessão iniciada,
                # não mostramos histórico.

                cur.execute("""
                    SELECT
                        roll,
                        color,
                        cor,
                        rodada_id
                    FROM blaze_historico
                    WHERE FALSE;
                """)

            rows = list(reversed(cur.fetchall()))

            vazio["total_jogos"] = len(rows)

            for roll, color, cor, rodada_id in rows:

                info = cor_info(color, cor)

                vazio["jogos"].append({

                    "roll": roll,

                    "color": color,

                    "cor": cor,

                    "rodada": rodada_id,

                    **info

                })

            # ----------------------------------------------------
            # HISTÓRICO DE SINAIS DA SESSÃO
            # ----------------------------------------------------

            if inicio_sessao:

                cur.execute("""
                    SELECT
                        estrategia,
                        cor_prevista,
                        rodada_base,
                        rodada_resultado,
                        cor_resultado,
                        resultado,
                        criado_em
                    FROM estrategia_sinais
                    WHERE criado_em >= %s
                    ORDER BY id DESC
                    LIMIT 12;
                """, (inicio_sessao,))

                for row in cur.fetchall():

                    (
                        estrategia,
                        prevista,
                        base,
                        rodada_resultado,
                        cor_resultado,
                        resultado,
                        criado
                    ) = row

                    vazio["historico_sinais"].append({

                        "estrategia":
                            estrategia_curta(estrategia),

                        "estrategia_full":
                            estrategia,

                        "prevista":
                            prevista,

                        "base":
                            base,

                        "rodada_resultado":
                            rodada_resultado,

                        "cor_resultado":
                            cor_resultado,

                        "resultado":
                            resultado,

                        "criado_em":
                            criado
                    })

        conn.close()

        return vazio

    except Exception as e:

        print(
            f"❌ Erro dashboard: {e}",
            flush=True
        )

        try:

            conn.rollback()
            conn.close()

        except:
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

    letter-spacing: -.5px;

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

    position: relative;

    overflow: hidden;

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

.signal.wait {

    border-color:
        rgba(255,255,255,.08);

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
        repeat(5, 1fr);

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

.result-white {

    background:
        rgba(255,255,255,.10);

    color: var(--white);

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

    transition: .2s;

    box-shadow:
        0 4px 15px
        rgba(0,0,0,0.3);

}

.control-btn:hover {

    transform:
        translateY(-2px);

    box-shadow:
        0 6px 20px
        rgba(0,0,0,0.4);

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
            repeat(3,1fr);

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

    <!-- ====================================================== -->
    <!-- CABEÇALHO -->
    <!-- ====================================================== -->

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


    <!-- ====================================================== -->
    <!-- CARDS SUPERIORES -->
    <!-- ====================================================== -->

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
                Motor de padrões
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

                {{ status.motor.whites }} branco(s)

            </div>

        </div>

    </div>


    <!-- ====================================================== -->
    <!-- SINAL ATUAL -->
    <!-- ====================================================== -->

    {% if status.motor.ativo and status.motor.sinal %}

        <div class="signal active">

            <div class="eyebrow">

                🎯 SINAL ATUAL
                • ENTRADA NA PRÓXIMA RODADA

            </div>

            {% if status.motor.cor == 'R' %}

                <div class="color red">
                    🔴 VERMELHO
                </div>

            {% elif status.motor.cor == 'P' %}

                <div class="color black">
                    ⚫ PRETO
                </div>

            {% else %}

                <div class="color white">
                    ⚪ BRANCO
                </div>

            {% endif %}

            <div class="strategy">

                {{ status.motor.estrategia or 'Sinal ativo' }}

            </div>

            <div class="entry">

                Aguardando o resultado
                da próxima rodada.

            </div>

        </div>

    {% else %}

        <div class="signal wait">

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

                O bot continua analisando
                as novas rodadas.

            </div>

        </div>

    {% endif %}


    <!-- ====================================================== -->
    <!-- DESEMPENHO -->
    <!-- ====================================================== -->

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

            <div class="num white">

                {{ status.motor.whites }}

            </div>

            <div class="m-label">
                Brancos
            </div>

        </div>


        <div class="metric">

            <div class="num">

                {{
                    status.motor.wins
                    +
                    status.motor.losses
                    +
                    status.motor.whites
                }}

            </div>

            <div class="m-label">
                Sinais
            </div>

        </div>


        <div class="metric">

            <div class="num yellow">

                {% set total_res =
                    status.motor.wins
                    +
                    status.motor.losses
                    +
                    status.motor.whites
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


    <!-- ====================================================== -->
    <!-- HISTÓRICO DE SINAIS -->
    <!-- ====================================================== -->

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
                Previsão
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

                        {% if s.prevista == 'R' %}

                            <span class="red">
                                🔴 R
                            </span>

                        {% elif s.prevista == 'P' %}

                            <span class="black">
                                ⚫ P
                            </span>

                        {% else %}

                            <span>
                                ⚪ {{ s.prevista or '—' }}
                            </span>

                        {% endif %}

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

                        {% elif s.resultado == 'WHITE' %}

                            <span
                                class="result-badge result-white"
                            >
                                ⚪ WHITE
                            </span>

                        {% else %}

                            <span>
                                —
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


    <!-- ====================================================== -->
    <!-- ÚLTIMOS RESULTADOS -->
    <!-- ====================================================== -->

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


    <!-- ====================================================== -->
    <!-- RODAPÉ -->
    <!-- ====================================================== -->

    <div class="footer">

        Blaze Bot • coleta em tempo real •
        sessão independente •
        controle anti-repetição

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

    return render_template_string(
        HTML,
        status=consultar_dashboard()
    )


# ================================================================
# CONTROLE DO MOTOR
# ================================================================

@app.route(
    "/controle_motor",
    methods=["POST"]
)
def controle_motor():

    conn = get_db_connection()

    if not conn:

        return redirect("/")

    try:

        with conn.cursor() as cur:

            cur.execute("""
                SELECT motor_ativo
                FROM bot_estado
                WHERE id = 1;
            """)

            estado = cur.fetchone()

            if not estado:

                conn.close()
                return redirect("/")

            motor_ativo = bool(estado[0])

            # ====================================================
            # PAUSAR
            # ====================================================

            if motor_ativo:

                print(
                    "⏸️ Motor pausado pelo dashboard.",
                    flush=True
                )

                cur.execute("""
                    UPDATE bot_estado
                    SET
                        motor_ativo = FALSE,
                        sinal_ativo = FALSE,
                        cor_sinal = NULL,
                        ultima_estrategia = NULL,
                        ultima_rodada_sinal = NULL,
                        sinal_base_id = NULL,
                        atualizado_em = CURRENT_TIMESTAMP
                    WHERE id = 1;
                """)

            # ====================================================
            # INICIAR NOVA SESSÃO
            # ====================================================

            else:

                print(
                    "▶️ Iniciando NOVA SESSÃO do motor.",
                    flush=True
                )

                cur.execute("""
                    UPDATE bot_estado
                    SET

                        motor_ativo = TRUE,

                        sinal_ativo = FALSE,

                        cor_sinal = NULL,

                        ultima_estrategia = NULL,

                        wins = 0,

                        losses = 0,

                        whites = 0,

                        profit = 0.0,

                        inicio_sessao = CURRENT_TIMESTAMP,

                        ultima_rodada_sinal = NULL,

                        sinal_base_id = NULL,

                        atualizado_em = CURRENT_TIMESTAMP

                    WHERE id = 1;
                """)

        conn.commit()

    except Exception as e:

        print(
            f"❌ Erro ao alterar estado do motor: {e}",
            flush=True
        )

        try:
            conn.rollback()
        except:
            pass

    finally:

        try:
            conn.close()
        except:
            pass

    return redirect("/")


# ================================================================
# HEALTH CHECK
# ================================================================

@app.route("/health")
def health():

    return jsonify({
        "status": "ok"
    }), 200


# ================================================================
# STATUS JSON
# ================================================================

@app.route("/status")
def status():

    return jsonify(
        consultar_dashboard()
    ), 200


# ================================================================
# INICIAR COLLECTOR
# ================================================================

def iniciar_background_collector():

    try:

        from collector import iniciar_coletor_em_thread

        print(
            "🚀 Iniciando Collector em background...",
            flush=True
        )

        iniciar_coletor_em_thread()

    except Exception as e:

        print(
            f"❌ Não foi possível iniciar o collector: {e}",
            flush=True
        )


# ================================================================
# INICIAR MOTOR
# ================================================================

def iniciar_background_motor():

    print(
        "🧠 Iniciando Motor Estatístico em background...",
        flush=True
    )

    motor_de_padroes()


# ================================================================
# INICIALIZAÇÃO
# ================================================================

init_web_db()


# ================================================================
# THREAD DO COLLECTOR
# ================================================================

if os.getenv(
    "RUN_COLLECTOR",
    "true"
).lower() == "true":

    collector_thread = threading.Thread(
        target=iniciar_background_collector,
        name="collector-bootstrap",
        daemon=True
    )

    collector_thread.start()


# ================================================================
# THREAD DO MOTOR
# ================================================================

if os.getenv(
    "RUN_MOTOR",
    "true"
).lower() == "true":

    motor_thread = threading.Thread(
        target=iniciar_background_motor,
        name="motor-bootstrap",
        daemon=True
    )

    motor_thread.start()
