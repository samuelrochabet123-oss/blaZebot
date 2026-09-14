# ================================================================
# BLAZE BOT — DASHBOARD WEB + COLLECTOR + MOTOR ESTATÍSTICO
# VERSÃO CORRIGIDA — BLOQUEIO DE SINAIS DUPLICADOS
# BRANCO = LOSS
# ================================================================

import os
import threading
import time

import psycopg2
from flask import Flask, jsonify, render_template_string, request, redirect

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
            # HISTÓRICO
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

                    motor_ativo BOOLEAN DEFAULT TRUE,
                    sinal_ativo BOOLEAN DEFAULT FALSE,

                    cor_sinal VARCHAR(5),
                    ultima_estrategia VARCHAR(100),

                    wins INTEGER DEFAULT 0,
                    losses INTEGER DEFAULT 0,
                    whites INTEGER DEFAULT 0,

                    profit FLOAT DEFAULT 0.0,

                    atualizado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
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


            # ====================================================
            # NOVOS CAMPOS DE CONTROLE
            #
            # Esses campos impedem que o mesmo sinal seja criado
            # ou resolvido várias vezes.
            # ====================================================

            cur.execute("""
                ALTER TABLE bot_estado
                ADD COLUMN IF NOT EXISTS sinal_rodada_base VARCHAR(100);
            """)

            cur.execute("""
                ALTER TABLE bot_estado
                ADD COLUMN IF NOT EXISTS sinal_rodada_resultado VARCHAR(100);
            """)


            # ----------------------------------------------------
            # SINAIS DAS ESTRATÉGIAS
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


            # ====================================================
            # MIGRAÇÃO DOS ANTIGOS "WHITE"
            #
            # A partir desta versão:
            #
            # BRANCO = LOSS
            #
            # Portanto, sinais antigos marcados WHITE também
            # passam a ser LOSS.
            # ====================================================

            cur.execute("""
                UPDATE estrategia_sinais
                SET resultado = 'LOSS'
                WHERE resultado = 'WHITE';
            """)

            white_convertidos = cur.rowcount

            if white_convertidos > 0:
                print(
                    f"🔄 {white_convertidos} registro(s) antigo(s) "
                    f"WHITE convertido(s) para LOSS.",
                    flush=True
                )


            # ====================================================
            # ÍNDICES DE PROTEÇÃO CONTRA DUPLICAÇÃO
            # ====================================================

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_sinais_rodada_base
                ON estrategia_sinais(rodada_base);
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_sinais_rodada_resultado
                ON estrategia_sinais(rodada_resultado);
            """)


        conn.commit()

        conn.close()

        print("✅ Banco inicializado corretamente.", flush=True)

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
# MAPEAR COR
# ================================================================

def mapear_cor_letra(cor_str):

    if not cor_str:
        return None

    cor = cor_str.upper().strip()

    if "VERMELHO" in cor:
        return "V"

    if "PRETO" in cor:
        return "P"

    if "BRANCO" in cor:
        return "B"

    return None


# ================================================================
# MOTOR ESTATÍSTICO
# ================================================================

def motor_de_padroes():

    print(
        "🧠 Motor de Padrões Estatísticos iniciado em background...",
        flush=True
    )

    while True:

        conn = get_db_connection()

        if not conn:
            time.sleep(10)
            continue

        try:

            with conn.cursor() as cur:

                # =================================================
                # 1. VERIFICAR SE MOTOR ESTÁ ATIVO
                # =================================================

                cur.execute("""
                    SELECT motor_ativo
                    FROM bot_estado
                    WHERE id = 1;
                """)

                estado = cur.fetchone()

                if not estado or not estado[0]:

                    conn.close()

                    time.sleep(5)

                    continue


                # =================================================
                # 2. PEGAR ÚLTIMAS 5 RODADAS
                #
                # Antes eram apenas 3.
                # Agora buscamos 5 para termos margem para verificar
                # corretamente a rodada-base e a rodada seguinte.
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
                    LIMIT 5;
                """)

                rows = cur.fetchall()


                if len(rows) < 2:

                    conn.close()

                    time.sleep(5)

                    continue


                # Mais antiga -> mais recente
                hist = list(reversed(rows))


                # =================================================
                # 3. PEGAR ESTADO COMPLETO DO MOTOR
                # =================================================

                cur.execute("""
                    SELECT
                        motor_ativo,
                        sinal_ativo,
                        cor_sinal,
                        ultima_estrategia,
                        sinal_rodada_base,
                        sinal_rodada_resultado
                    FROM bot_estado
                    WHERE id = 1;
                """)

                estado_detalhado = cur.fetchone()


                if not estado_detalhado:

                    conn.close()

                    time.sleep(5)

                    continue


                (
                    motor_ativo,
                    sinal_ativo,
                    cor_sinal,
                    ultima_estrategia,
                    sinal_rodada_base,
                    sinal_rodada_resultado
                ) = estado_detalhado


                # =================================================
                # 4. SE EXISTE SINAL PENDENTE
                #
                # NÃO usamos simplesmente "jogo_atual".
                #
                # Procuramos explicitamente uma rodada posterior
                # à rodada-base.
                # =================================================

                if motor_ativo and sinal_ativo:

                    # ---------------------------------------------
                    # Rodada-base do sinal
                    # ---------------------------------------------

                    rodada_base = sinal_rodada_base


                    if not rodada_base:

                        print(
                            "⚠️ Sinal ativo sem rodada-base. "
                            "Cancelando sinal para proteção.",
                            flush=True
                        )

                        cur.execute("""
                            UPDATE bot_estado
                            SET
                                sinal_ativo = FALSE,
                                cor_sinal = NULL,
                                ultima_estrategia = NULL,
                                sinal_rodada_base = NULL,
                                sinal_rodada_resultado = NULL,
                                atualizado_em = CURRENT_TIMESTAMP
                            WHERE id = 1;
                        """)

                        conn.commit()

                        conn.close()

                        time.sleep(2)

                        continue


                    # ---------------------------------------------
                    # Procurar rodada posterior à base
                    # ---------------------------------------------

                    cur.execute("""
                        SELECT
                            id,
                            rodada_id,
                            cor,
                            color,
                            roll
                        FROM blaze_historico
                        WHERE status = 'complete'
                          AND id > (
                              SELECT id
                              FROM blaze_historico
                              WHERE rodada_id = %s
                              LIMIT 1
                          )
                        ORDER BY id ASC
                        LIMIT 1;
                    """, (rodada_base,))


                    resultado_row = cur.fetchone()


                    # Ainda não chegou uma nova rodada
                    if not resultado_row:

                        conn.close()

                        time.sleep(2)

                        continue


                    (
                        resultado_id,
                        rodada_resultado,
                        cor_resultado_texto,
                        color_resultado,
                        roll_resultado
                    ) = resultado_row


                    # =================================================
                    # 5. PROTEÇÃO EXTRA:
                    #
                    # Se esta rodada já foi usada para resolver o
                    # sinal, NÃO processa novamente.
                    # =================================================

                    if sinal_rodada_resultado == rodada_resultado:

                        conn.close()

                        time.sleep(2)

                        continue


                    # =================================================
                    # 6. VERIFICAR SE JÁ EXISTE ESTE SINAL NO BANCO
                    #
                    # Segunda camada de segurança.
                    # =================================================

                    cur.execute("""
                        SELECT id
                        FROM estrategia_sinais
                        WHERE rodada_base = %s
                          AND rodada_resultado = %s
                        LIMIT 1;
                    """, (
                        rodada_base,
                        rodada_resultado
                    ))

                    sinal_existente = cur.fetchone()


                    if sinal_existente:

                        print(
                            f"⚠️ Sinal já registrado: "
                            f"base={rodada_base} "
                            f"resultado={rodada_resultado}",
                            flush=True
                        )

                        cur.execute("""
                            UPDATE bot_estado
                            SET
                                sinal_ativo = FALSE,
                                sinal_rodada_resultado = %s,
                                atualizado_em = CURRENT_TIMESTAMP
                            WHERE id = 1;
                        """, (rodada_resultado,))

                        conn.commit()

                        conn.close()

                        time.sleep(2)

                        continue


                    # =================================================
                    # 7. CONVERTER RESULTADO
                    # =================================================

                    cor_real_letra = mapear_cor_letra(
                        cor_resultado_texto
                    )


                    # =================================================
                    # 8. REGRA CORRETA:
                    #
                    # V previsão + V = WIN
                    # V previsão + P = LOSS
                    # V previsão + B = LOSS
                    #
                    # P previsão + P = WIN
                    # P previsão + V = LOSS
                    # P previsão + B = LOSS
                    # =================================================

                    if cor_real_letra == cor_sinal:

                        resultado_status = "WIN"

                    else:

                        # BRANCO também entra aqui como LOSS
                        resultado_status = "LOSS"


                    # =================================================
                    # 9. REGISTRAR SINAL
                    # =================================================

                    cur.execute("""
                        INSERT INTO estrategia_sinais (
                            estrategia,
                            cor_prevista,
                            rodada_base,
                            rodada_resultado,
                            cor_resultado,
                            resultado,
                            criado_em
                        )
                        VALUES (
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
                        rodada_base,
                        rodada_resultado,
                        cor_resultado_texto,
                        resultado_status
                    ))


                    # =================================================
                    # 10. ATUALIZAR PLACAR
                    # =================================================

                    if resultado_status == "WIN":

                        cur.execute("""
                            UPDATE bot_estado
                            SET
                                wins = wins + 1,
                                profit = profit + 1.0,
                                sinal_ativo = FALSE,

                                sinal_rodada_resultado = %s,

                                atualizado_em = CURRENT_TIMESTAMP
                            WHERE id = 1;
                        """, (rodada_resultado,))


                        print(
                            f"🟢 WIN | "
                            f"{ultima_estrategia} | "
                            f"Base: {rodada_base} | "
                            f"Resultado: {rodada_resultado} | "
                            f"Cor: {cor_resultado_texto}",
                            flush=True
                        )


                    else:

                        cur.execute("""
                            UPDATE bot_estado
                            SET
                                losses = losses + 1,
                                profit = profit - 1.0,
                                sinal_ativo = FALSE,

                                sinal_rodada_resultado = %s,

                                atualizado_em = CURRENT_TIMESTAMP
                            WHERE id = 1;
                        """, (rodada_resultado,))


                        print(
                            f"🔴 LOSS | "
                            f"{ultima_estrategia} | "
                            f"Base: {rodada_base} | "
                            f"Resultado: {rodada_resultado} | "
                            f"Cor: {cor_resultado_texto}",
                            flush=True
                        )


                    conn.commit()

                    conn.close()

                    time.sleep(2)

                    continue


                # =================================================
                # 11. MOTOR ATIVO SEM SINAL
                #
                # Procurar novo gatilho.
                # =================================================

                if motor_ativo and not sinal_ativo:

                    # Precisamos de pelo menos duas rodadas
                    if len(hist) < 2:

                        conn.close()

                        time.sleep(5)

                        continue


                    rodada_anterior = hist[-2]
                    rodada_atual = hist[-1]


                    cor_anterior = mapear_cor_letra(
                        rodada_anterior[2]
                    )

                    cor_atual = mapear_cor_letra(
                        rodada_atual[2]
                    )


                    if not cor_anterior or not cor_atual:

                        conn.close()

                        time.sleep(5)

                        continue


                    # =================================================
                    # GATILHO:
                    #
                    # PRETO + PRETO
                    #
                    # -> sinal VERMELHO na próxima rodada
                    # =================================================

                    seq_2 = cor_anterior + cor_atual


                    if seq_2 == "PP":

                        nova_rodada_base = rodada_atual[1]


                        # =================================================
                        # PROTEÇÃO PRINCIPAL CONTRA DUPLICAÇÃO
                        #
                        # Se esta rodada já gerou sinal anteriormente,
                        # NÃO gera novamente.
                        # =================================================

                        if (
                            sinal_rodada_base is not None
                            and sinal_rodada_base == nova_rodada_base
                        ):

                            conn.close()

                            time.sleep(3)

                            continue


                        # =================================================
                        # PROTEÇÃO EXTRA NO BANCO
                        # =================================================

                        cur.execute("""
                            SELECT id
                            FROM estrategia_sinais
                            WHERE rodada_base = %s
                            LIMIT 1;
                        """, (nova_rodada_base,))


                        base_ja_usada = cur.fetchone()


                        if base_ja_usada:

                            # Atualiza a memória do motor para não ficar
                            # consultando novamente essa mesma rodada.

                            cur.execute("""
                                UPDATE bot_estado
                                SET
                                    sinal_rodada_base = %s,
                                    atualizado_em = CURRENT_TIMESTAMP
                                WHERE id = 1;
                            """, (nova_rodada_base,))

                            conn.commit()

                            conn.close()

                            time.sleep(3)

                            continue


                        # =================================================
                        # CRIAR NOVO SINAL
                        # =================================================

                        cur.execute("""
                            UPDATE bot_estado
                            SET
                                sinal_ativo = TRUE,
                                cor_sinal = 'R',
                                ultima_estrategia =
                                    'EST DATA (2x Preto -> V)',

                                sinal_rodada_base = %s,
                                sinal_rodada_resultado = NULL,

                                atualizado_em = CURRENT_TIMESTAMP

                            WHERE id = 1;
                        """, (nova_rodada_base,))


                        conn.commit()


                        print(
                            f"🎯 NOVO SINAL | "
                            f"EST DATA (2x Preto -> V) | "
                            f"Base: {nova_rodada_base} | "
                            f"Previsão: VERMELHO",
                            flush=True
                        )


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


        time.sleep(5)


# ================================================================
# ESTRATÉGIAS
# ================================================================

ESTRATEGIAS = [

    "EST 1 (Sniper Par)",
    "EST 1 (Sniper Ímpar)",
    "EST 2 (Operacional)",
    "EST 3 (Franco-Atirador)",
    "EST 4 (Bala de Prata)",
    "EST 5 (Mina Oculta)",
    "EST DATA (2x Preto -> V)"

]


# ================================================================
# INFORMAÇÕES DE COR
# ================================================================

def cor_info(color, cor_texto=None):

    try:
        color = int(color)
    except:
        color = None


    if color == 0:

        return {
            "sigla": "W",
            "nome": "BRANCO",
            "classe": "white",
            "emoji": "⚪"
        }


    if color == 1:

        return {
            "sigla": "R",
            "nome": "VERMELHO",
            "classe": "red",
            "emoji": "🔴"
        }


    if color == 2:

        return {
            "sigla": "B",
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
            "sigla": "B",
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
            "EST 1 (Sniper Par)",
            "EST 1 • Sniper Par"
        )

        .replace(
            "EST 1 (Sniper Ímpar)",
            "EST 1 • Sniper Ímpar"
        )

        .replace(
            "EST 2 (Operacional)",
            "EST 2 • Operacional"
        )

        .replace(
            "EST 3 (Franco-Atirador)",
            "EST 3 • Franco-Atirador"
        )

        .replace(
            "EST 4 (Bala de Prata)",
            "EST 4 • Bala de Prata"
        )

        .replace(
            "EST 5 (Mina Oculta)",
            "EST 5 • Mina Oculta"
        )

        .replace(
            "EST DATA (2x Preto -> V)",
            "📊 EST DATA • 2P -> V"
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
            "sinal": None,
            "cor": None,
            "estrategia": None,

            "wins": 0,
            "losses": 0,
            "whites": 0,

            "profit": 0.0
        },

        "estrategias": [],

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
            # TOTAL DE JOGOS
            # ----------------------------------------------------

            cur.execute("""
                SELECT COUNT(*)
                FROM blaze_historico;
            """)

            vazio["total_jogos"] = cur.fetchone()[0]


            # ----------------------------------------------------
            # ÚLTIMOS 24 RESULTADOS
            # ----------------------------------------------------

            cur.execute("""
                SELECT
                    roll,
                    color,
                    cor,
                    rodada_id

                FROM blaze_historico

                WHERE status = 'complete'

                ORDER BY id DESC

                LIMIT 24;
            """)


            rows = list(
                reversed(cur.fetchall())
            )


            for roll, color, cor, rodada_id in rows:

                info = cor_info(
                    color,
                    cor
                )


                vazio["jogos"].append({

                    "roll": roll,

                    "color": color,

                    "cor": cor,

                    "rodada": rodada_id,

                    **info

                })


            # ----------------------------------------------------
            # ESTADO DO MOTOR
            # ----------------------------------------------------

            try:

                cur.execute("""
                    SELECT
                        motor_ativo,
                        sinal_ativo,
                        cor_sinal,
                        ultima_estrategia,
                        wins,
                        losses,
                        whites,
                        profit

                    FROM bot_estado

                    WHERE id = 1;
                """)


                row = cur.fetchone()


                if row:

                    vazio["motor"] = {

                        "ativo": bool(row[0]),

                        "sinal": row[1],

                        "cor": row[2],

                        "estrategia": row[3],

                        "wins": row[4] or 0,

                        "losses": row[5] or 0,

                        "whites": row[6] or 0,

                        "profit": float(
                            row[7] or 0
                        )

                    }


            except Exception:

                conn.rollback()


            # ----------------------------------------------------
            # PERFORMANCE DAS ESTRATÉGIAS
            #
            # WHITE antigo já foi convertido para LOSS.
            # ----------------------------------------------------

            try:

                cur.execute("""
                    SELECT
                        estrategia,

                        COUNT(*) AS sinais,

                        COUNT(*) FILTER (
                            WHERE resultado = 'WIN'
                        ) AS wins,

                        COUNT(*) FILTER (
                            WHERE resultado = 'LOSS'
                        ) AS losses,

                        COUNT(*) FILTER (
                            WHERE resultado = 'WHITE'
                        ) AS whites,

                        COUNT(*) FILTER (
                            WHERE resultado = 'PENDENTE'
                        ) AS pendentes

                    FROM estrategia_sinais

                    GROUP BY estrategia;
                """)


                perf_rows = cur.fetchall()


            except Exception:

                conn.rollback()

                perf_rows = []


            perf_map = {}


            for (
                nome,
                sinais,
                wins,
                losses,
                whites,
                pendentes
            ) in perf_rows:


                resolvidos = (
                    (wins or 0)
                    +
                    (losses or 0)
                    +
                    (whites or 0)
                )


                taxa = (

                    wins / resolvidos * 100

                ) if resolvidos else 0


                perf_map[nome] = {

                    "nome": nome,

                    "curto": estrategia_curta(nome),

                    "sinais": sinais or 0,

                    "wins": wins or 0,

                    "losses": losses or 0,

                    "whites": whites or 0,

                    "pendentes": pendentes or 0,

                    "taxa": round(
                        taxa,
                        1
                    )

                }


            # ----------------------------------------------------
            # GARANTIR TODAS AS ESTRATÉGIAS NO DASHBOARD
            # ----------------------------------------------------

            for nome in ESTRATEGIAS:

                vazio["estrategias"].append(

                    perf_map.get(
                        nome,
                        {
                            "nome": nome,

                            "curto":
                                estrategia_curta(nome),

                            "sinais": 0,

                            "wins": 0,

                            "losses": 0,

                            "whites": 0,

                            "pendentes": 0,

                            "taxa": 0
                        }
                    )

                )


            # ----------------------------------------------------
            # ÚLTIMOS SINAIS
            # ----------------------------------------------------

            try:

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

                    ORDER BY id DESC

                    LIMIT 12;
                """)


                for r in cur.fetchall():

                    (
                        estrategia,
                        prevista,
                        base,
                        resultado_rodada,
                        cor_resultado,
                        resultado,
                        criado
                    ) = r


                    vazio["historico_sinais"].append({

                        "estrategia":
                            estrategia_curta(
                                estrategia
                            ),

                        "estrategia_full":
                            estrategia,

                        "prevista":
                            prevista,

                        "base":
                            base,

                        "rodada_resultado":
                            resultado_rodada,

                        "cor_resultado":
                            cor_resultado,

                        "resultado":
                            resultado,

                        "criado_em":
                            criado

                    })


            except Exception:

                conn.rollback()


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

    --blue: #6ea8ff;

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

    display:flex;

    justify-content:space-between;

    align-items:center;

    margin-bottom:16px;

    gap:15px;

    flex-wrap:wrap;

}

.logo {

    font-size:25px;

    font-weight:900;

    letter-spacing:-.5px;

}

.logo span {

    color:var(--red);

}

.header-right {

    display:flex;

    align-items:center;

    gap:15px;

}

.live {

    display:flex;

    align-items:center;

    gap:8px;

    font-size:12px;

    font-weight:900;

    color:var(--green);

}

.dot {

    width:9px;

    height:9px;

    border-radius:50%;

    background:currentColor;

    box-shadow:0 0 12px currentColor;

}

.top-grid {

    display:grid;

    grid-template-columns:
        1.2fr 1fr 1fr 1fr;

    gap:10px;

    margin-bottom:12px;

}

.card {

    background:
        rgba(16,20,27,.92);

    border:
        1px solid var(--border);

    border-radius:15px;

    padding:15px;

}

.label {

    color:var(--muted);

    font-size:10px;

    font-weight:900;

    text-transform:uppercase;

    letter-spacing:.7px;

    margin-bottom:7px;

}

.big {

    font-size:23px;

    font-weight:950;

}

.small {

    color:var(--muted);

    font-size:11px;

    margin-top:5px;

}

.green {

    color:var(--green);

}

.red {

    color:var(--red);

}

.yellow {

    color:var(--yellow);

}

.white {

    color:var(--white);

}

.black {

    color:#d7dce3;

}

.signal {

    position:relative;

    overflow:hidden;

    margin-bottom:12px;

    padding:24px;

    border-radius:18px;

    background:
        linear-gradient(
            145deg,
            rgba(22,27,36,.98),
            rgba(12,15,21,.98)
        );

    border:
        1px solid
        rgba(255,255,255,.10);

    text-align:center;

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

    color:var(--muted);

    font-size:11px;

    font-weight:900;

    text-transform:uppercase;

    letter-spacing:1px;

}

.signal .color {

    font-size:38px;

    font-weight:950;

    margin:7px 0 4px;

}

.signal .strategy {

    font-size:15px;

    font-weight:850;

}

.signal .entry {

    margin-top:9px;

    color:var(--muted);

    font-size:12px;

}

.section-title {

    display:flex;

    justify-content:space-between;

    align-items:end;

    margin:18px 0 9px;

}

.section-title h2 {

    font-size:13px;

    text-transform:uppercase;

    letter-spacing:.8px;

}

.section-title span {

    color:var(--muted);

    font-size:11px;

}

.performance {

    display:grid;

    grid-template-columns:
        repeat(6, 1fr);

    gap:9px;

    margin-bottom:12px;

}

.metric {

    text-align:center;

    padding:13px 8px;

    background:
        rgba(16,20,27,.92);

    border:
        1px solid var(--border);

    border-radius:13px;

}

.metric .num {

    font-size:22px;

    font-weight:950;

}

.metric .m-label {

    margin-top:4px;

    color:var(--muted);

    font-size:9px;

    text-transform:uppercase;

    font-weight:900;

}

.strategy-grid {

    display:grid;

    grid-template-columns:
        repeat(3, 1fr);

    gap:10px;

}

.strategy {

    background:
        rgba(16,20,27,.92);

    border:
        1px solid var(--border);

    border-radius:15px;

    padding:14px;

}

.strategy-head {

    display:flex;

    justify-content:space-between;

    gap:10px;

    align-items:start;

}

.strategy-name {

    font-size:13px;

    font-weight:900;

}

.badge {

    font-size:8px;

    font-weight:900;

    padding:5px 7px;

    border-radius:999px;

    background:
        rgba(35,226,124,.10);

    color:var(--green);

    white-space:nowrap;

}

.strategy-stats {

    display:grid;

    grid-template-columns:
        repeat(4,1fr);

    gap:6px;

    margin-top:12px;

}

.strategy-stat {

    background:
        rgba(255,255,255,.025);

    border-radius:9px;

    padding:7px 4px;

    text-align:center;

}

.strategy-stat b {

    display:block;

    font-size:14px;

}

.strategy-stat span {

    display:block;

    color:var(--muted);

    font-size:8px;

    margin-top:2px;

    text-transform:uppercase;

}

.history {

    background:
        rgba(16,20,27,.92);

    border:
        1px solid var(--border);

    border-radius:15px;

    overflow:hidden;

}

.history-row {

    display:grid;

    grid-template-columns:
        1.7fr .7fr 1fr 1fr 1fr;

    gap:8px;

    align-items:center;

    padding:11px 13px;

    border-bottom:
        1px solid
        rgba(255,255,255,.045);

    font-size:11px;

}

.history-row:last-child {

    border-bottom:0;

}

.history-head {

    color:var(--muted);

    font-size:9px;

    font-weight:900;

    text-transform:uppercase;

}

.result-badge {

    display:inline-flex;

    align-items:center;

    justify-content:center;

    min-width:58px;

    padding:5px 7px;

    border-radius:7px;

    font-size:9px;

    font-weight:950;

}

.result-win {

    background:
        rgba(35,226,124,.12);

    color:var(--green);

}

.result-loss {

    background:
        rgba(255,77,93,.12);

    color:var(--red);

}

.result-white {

    background:
        rgba(255,255,255,.10);

    color:var(--white);

}

.result-pending {

    background:
        rgba(255,200,87,.12);

    color:var(--yellow);

}

.results {

    display:flex;

    gap:7px;

    overflow-x:auto;

    padding:12px;

    background:
        rgba(16,20,27,.92);

    border:
        1px solid var(--border);

    border-radius:15px;

}

.pill {

    flex:
        0 0 auto;

    width:48px;

    height:48px;

    border-radius:10px;

    display:flex;

    flex-direction:column;

    align-items:center;

    justify-content:center;

    font-weight:950;

}

.pill small {

    font-size:8px;

    opacity:.75;

    margin-top:2px;

}

.pill.red-bg {

    background:var(--red);

    color:white;

}

.pill.black-bg {

    background:#262c35;

    color:white;

    border:
        1px solid #444b55;

}

.pill.white-bg {

    background:white;

    color:#111;

}

.footer {

    text-align:center;

    color:#56606d;

    font-size:9px;

    padding:18px 0 4px;

}

.control-btn {

    border:none;

    padding:12px 24px;

    border-radius:12px;

    font-weight:900;

    cursor:pointer;

    font-size:14px;

    color:white;

    transition:.2s;

    box-shadow:
        0 4px 15px
        rgba(0,0,0,0.3);

}

.control-btn:hover {

    transform:translateY(-2px);

    box-shadow:
        0 6px 20px
        rgba(0,0,0,0.4);

}

.control-btn.start {

    background:var(--green);

}

.control-btn.pause {

    background:var(--red);

}

@media (max-width:900px) {

    .top-grid {

        grid-template-columns:
            repeat(2,1fr);

    }

    .performance {

        grid-template-columns:
            repeat(3,1fr);

    }

    .strategy-grid {

        grid-template-columns:
            repeat(2,1fr);

    }

}

@media (max-width:600px) {

    body {

        padding:12px;

    }

    .top-grid,
    .strategy-grid {

        grid-template-columns:1fr;

    }

    .performance {

        grid-template-columns:
            repeat(2,1fr);

    }

    .history-row {

        grid-template-columns:
            1.4fr .7fr 1fr 1fr;

    }

    .history-row .hide-mobile {

        display:none;

    }

}

</style>

</head>


<body>

<div class="container">


    <!-- ===================================================== -->
    <!-- HEADER -->
    <!-- ===================================================== -->

    <div class="header">

        <div class="logo">
            🤖 Blaze <span>Bot</span>
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


    <!-- ===================================================== -->
    <!-- CARDS SUPERIORES -->
    <!-- ===================================================== -->

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
                Jogos no histórico
            </div>

            <div class="big yellow">
                {{ status.total_jogos }}
            </div>

            <div class="small">
                Neon PostgreSQL
            </div>

        </div>


        <div class="card">

            <div class="label">
                Motor de estratégias
            </div>

            {% if status.motor.ativo %}

                <div class="big green">
                    🟢 ATIVO
                </div>

            {% else %}

                <div class="big red">
                    🔴 INATIVO
                </div>

            {% endif %}

            <div class="small">
                Estratégias carregadas
            </div>

        </div>


        <div class="card">

            <div class="label">
                Resultado do último sinal
            </div>

            {% if
                status.motor.wins
                + status.motor.losses
                + status.motor.whites
                > 0
            %}

                <div class="big">

                    {{ status.motor.wins }}W /
                    {{ status.motor.losses }}L

                </div>

                <div class="small">

                    {{ status.motor.whites }}
                    branco(s)

                </div>

            {% else %}

                <div class="big">
                    —
                </div>

                <div class="small">
                    Nenhum sinal resolvido
                </div>

            {% endif %}

        </div>

    </div>


    <!-- ===================================================== -->
    <!-- SINAL ATUAL -->
    <!-- ===================================================== -->

    {% if
        status.motor.ativo
        and status.motor.sinal
    %}

        <div class="signal active">

            <div class="eyebrow">

                🎯 SINAL ATUAL • ENTRADA NA PRÓXIMA RODADA

            </div>


            {% if status.motor.cor == 'R' %}

                <div class="color red">
                    🔴 VERMELHO
                </div>

            {% elif status.motor.cor == 'B' %}

                <div class="color black">
                    ⚫ PRETO
                </div>

            {% else %}

                <div class="color white">
                    ⚪ BRANCO
                </div>

            {% endif %}


            <div class="strategy">

                {{ status.motor.estrategia or status.motor.sinal }}

            </div>


            <div class="entry">

                Previsão:
                <strong>
                    {{ status.motor.cor }}
                </strong>

                • aguardando o próximo resultado
                para resolver o sinal

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

                    Nenhuma estratégia encontrou padrão.

                {% else %}

                    Motor Pausado.

                {% endif %}

            </div>


            <div class="entry">

                Isso é normal:
                o bot continua analisando cada nova rodada.

            </div>

        </div>


    {% endif %}


    <!-- ===================================================== -->
    <!-- DESEMPENHO -->
    <!-- ===================================================== -->

    <div class="section-title">

        <h2>
            📊 Desempenho geral
        </h2>

        <span>
            Todos os sinais resolvidos
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
                            * 100
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


        <div class="metric">

            <div
                class="num
                {% if status.motor.profit >= 0 %}
                    green
                {% else %}
                    red
                {% endif %}"
            >

                {{
                    '%+.2f'|format(
                        status.motor.profit
                    )
                }}

            </div>

            <div class="m-label">
                Resultado base
            </div>

        </div>


    </div>


    <!-- ===================================================== -->
    <!-- ESTRATÉGIAS -->
    <!-- ===================================================== -->

    <div class="section-title">

        <h2>
            🧠 Estratégias
        </h2>

        <span>
            Habilitadas no motor
        </span>

    </div>


    <div class="strategy-grid">


        {% for est in status.estrategias %}

        <div class="strategy">


            <div class="strategy-head">

                <div class="strategy-name">
                    {{ est.curto }}
                </div>

                <div class="badge">
                    ● HABILITADA
                </div>

            </div>


            <div class="strategy-stats">


                <div class="strategy-stat">

                    <b>
                        {{ est.sinais }}
                    </b>

                    <span>
                        Sinais
                    </span>

                </div>


                <div class="strategy-stat">

                    <b class="green">
                        {{ est.wins }}
                    </b>

                    <span>
                        Wins
                    </span>

                </div>


                <div class="strategy-stat">

                    <b class="red">
                        {{ est.losses }}
                    </b>

                    <span>
                        Losses
                    </span>

                </div>


                <div class="strategy-stat">

                    <b class="yellow">
                        {{ est.taxa }}%
                    </b>

                    <span>
                        Acerto
                    </span>

                </div>


            </div>

        </div>

        {% endfor %}


    </div>


    <!-- ===================================================== -->
    <!-- HISTÓRICO DE SINAIS -->
    <!-- ===================================================== -->

    <div class="section-title">

        <h2>
            🧾 Últimos sinais
        </h2>

        <span>
            Mais recente primeiro
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

                    {% elif s.prevista == 'B' %}

                        <span class="black">
                            ⚫ B
                        </span>

                    {% else %}

                        <span>
                            ⚪
                            {{ s.prevista or '—' }}
                        </span>

                    {% endif %}

                </div>


                <div>
                    {{ s.base }}
                </div>


                <div>

                    {% if s.resultado == 'WIN' %}

                        <span
                            class="
                                result-badge
                                result-win
                            "
                        >
                            ✓ WIN
                        </span>


                    {% elif s.resultado == 'LOSS' %}

                        <span
                            class="
                                result-badge
                                result-loss
                            "
                        >
                            ✕ LOSS
                        </span>


                    {% elif s.resultado == 'WHITE' %}

                        <span
                            class="
                                result-badge
                                result-white
                            "
                        >
                            ⚪ WHITE
                        </span>


                    {% else %}

                        <span
                            class="
                                result-badge
                                result-pending
                            "
                        >
                            ⏳ PENDENTE
                        </span>

                    {% endif %}

                </div>


                <div class="hide-mobile">

                    {{ s.rodada_resultado or '—' }}

                </div>


            </div>


            {% endfor %}


        {% else %}


            <div
                style="
                    padding:20px;
                    text-align:center;
                    color:#8993a1;
                    font-size:12px
                "
            >
                Ainda não há sinais registrados.
            </div>


        {% endif %}


    </div>


    <!-- ===================================================== -->
    <!-- ÚLTIMOS RESULTADOS -->
    <!-- ===================================================== -->

    <div class="section-title">

        <h2>
            🎲 Últimos resultados
        </h2>

        <span>
            24 rodadas mais recentes
        </span>

    </div>


    <div class="results">


        {% for jogo in status.jogos %}


            <div
                class="
                    pill

                    {% if jogo.sigla == 'R' %}
                        red-bg

                    {% elif jogo.sigla == 'B' %}
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


    </div>


    <div class="footer">

        Blaze Bot • coleta em tempo real •
        motor de estratégias validado estatisticamente •
        dashboard atualizado automaticamente

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

                return redirect("/")


            motor_ativo = estado[0]


            # ====================================================
            # PAUSAR
            # ====================================================

            if motor_ativo:

                cur.execute("""
                    UPDATE bot_estado

                    SET
                        motor_ativo = FALSE,

                        sinal_ativo = FALSE,

                        cor_sinal = NULL,

                        ultima_estrategia = NULL,

                        sinal_rodada_resultado = NULL,

                        atualizado_em =
                            CURRENT_TIMESTAMP

                    WHERE id = 1;
                """)


                print(
                    "⏸️ Motor PAUSADO pelo usuário.",
                    flush=True
                )


            # ====================================================
            # INICIAR
            # ====================================================

            else:

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

                        sinal_rodada_base = NULL,

                        sinal_rodada_resultado = NULL,

                        atualizado_em =
                            CURRENT_TIMESTAMP

                    WHERE id = 1;
                """)


                print(
                    "▶️ Motor INICIADO pelo usuário. "
                    "Placar zerado.",
                    flush=True
                )


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

        conn.close()


    return redirect("/")


# ================================================================
# HEALTH
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

        from collector import (
            iniciar_coletor_em_thread
        )

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
