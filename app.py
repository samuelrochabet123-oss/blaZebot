# ================================================================
# BLAZE BOT — DASHBOARD WEB + COLLECTOR + MOTOR ESTATÍSTICO
#
# VERSÃO:
# - Remove estratégia antiga 2x Preto -> Vermelho
# - Novas estratégias:
#
#   1) VI -> VI -> R
#   2) VI -> PP -> P
#   3) VI -> VI -> VI -> R
#
# - Regra adicional W + 13
#
# CORREÇÕES IMPORTANTES:
#
# 1. O sinal é baseado em uma rodada específica.
# 2. O resultado só pode ser uma rodada POSTERIOR ao sinal_base_id.
# 3. A própria rodada que gerou o sinal NUNCA pode resolver o sinal.
# 4. Branco NÃO entra no placar.
# 5. Branco NÃO é contabilizado como WIN/LOSS.
# 6. O collector continua coletando mesmo com o motor parado.
# 7. INICIAR cria uma nova sessão estatística.
# 8. PAUSAR não para o collector.
# 9. Proteção contra repetição da mesma rodada-base.
# 10. Estratégias têm prioridade definida.
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

                    sinal_ativo BOOLEAN DEFAULT FALSE,

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

                    sinal_base_id INTEGER
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
                        DEFAULT CURRENT_TIMESTAMP
                );
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
# MOTOR DE ESTRATÉGIAS
# ================================================================

def detectar_estrategia(hist):

    """
    Recebe histórico em ordem cronológica.

    Exemplo:

        R P P R

    hist = [
        {
            "id": ...,
            "rodada_id": ...,
            "cor": "R",
            "roll": ...
        },
        ...
    ]

    Retorna:

        (cor_prevista, nome_estrategia)

    ou:

        (None, None)
    """

    if not hist:
        return None, None


    # ============================================================
    # ESTRATÉGIA 1
    #
    # VI -> VI -> R
    #
    # Aqui VI é tratado como VERMELHO.
    #
    # Portanto:
    #
    # R
    # R
    # ↓
    # previsão R
    #
    # ============================================================

    if len(hist) >= 2:

        a = hist[-2]["cor"]
        b = hist[-1]["cor"]

        if a == "R" and b == "R":

            return (
                "R",
                "VI → VI → R"
            )


    # ============================================================
    # ESTRATÉGIA 2
    #
    # VI -> PP -> P
    #
    # Interpretação:
    #
    # R + P + P
    #       ↓
    # previsão P
    #
    # ============================================================

    if len(hist) >= 3:

        a = hist[-3]["cor"]
        b = hist[-2]["cor"]
        c = hist[-1]["cor"]

        if (
            a == "R"
            and b == "P"
            and c == "P"
        ):

            return (
                "P",
                "VI → PP → P"
            )


    # ============================================================
    # ESTRATÉGIA 3
    #
    # VI -> VI -> VI -> R
    #
    # Interpretação:
    #
    # R + R + R
    #         ↓
    # previsão R
    #
    # OBS:
    #
    # Essa regra é mais longa e pode ficar escondida pela
    # estratégia VI -> VI -> R.
    #
    # Por isso ela é testada ANTES da regra de 2 VI.
    #
    # ============================================================

    if len(hist) >= 3:

        a = hist[-3]["cor"]
        b = hist[-2]["cor"]
        c = hist[-1]["cor"]

        if (
            a == "R"
            and b == "R"
            and c == "R"
        ):

            return (
                "R",
                "VI → VI → VI → R"
            )


    # ============================================================
    # W + 13
    #
    # Regra configurável.
    #
    # Interpretação adotada neste código:
    #
    # se a última rodada foi BRANCO e o roll da rodada branca
    # foi 0, procuramos o próximo evento específico relacionado
    # ao roll 13.
    #
    # IMPORTANTE:
    #
    # Como "W +13" não veio formalmente definido no código
    # original, não transformamos isso em uma aposta automática
    # sem uma definição objetiva da direção.
    #
    # Neste momento ela é registrada como gatilho apenas quando
    # houver uma regra explícita de previsão abaixo.
    #
    # ============================================================

    if len(hist) >= 1:

        ultima = hist[-1]

        if ultima["cor"] == "W":

            # ----------------------------------------------------
            # PLACEHOLDER CONTROLADO
            #
            # Para evitar criar uma previsão arbitrária,
            # NÃO gera sinal automaticamente.
            #
            # Se o estudo W+13 significar, por exemplo:
            #
            # W -> próximo roll 13 -> PRETO
            #
            # essa regra pode ser ativada aqui.
            # ----------------------------------------------------

            pass


    return None, None


# ================================================================
# CONVERTE ROW DO BANCO PARA ESTRUTURA DO MOTOR
# ================================================================

def row_para_hist(row):

    return {

        "id": row[0],

        "rodada_id": row[1],

        "cor": obter_cor(
            row[2],
            row[3]
        ),

        "roll": row[4]
    }


# ================================================================
# MOTOR DE PADRÕES
# ================================================================

def motor_de_padroes():

    print(
        "🧠 Motor Estatístico iniciado em background...",
        flush=True
    )

    while True:

        conn = get_db_connection()

        if not conn:

            time.sleep(5)

            continue

        try:

            with conn.cursor() as cur:

                # =================================================
                # ESTADO ATUAL
                # =================================================

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


                # =================================================
                # MOTOR PAUSADO
                #
                # IMPORTANTE:
                #
                # Aqui NÃO fazemos nada no collector.
                #
                # O collector roda em sua própria thread.
                #
                # Pausar = parar previsões.
                # Não parar coleta.
                # =================================================

                if not motor_ativo:

                    conn.close()

                    time.sleep(3)

                    continue


                # =================================================
                # 1. RESOLVER SINAL PENDENTE
                # =================================================

                if sinal_ativo and sinal_base_id:

                    # ------------------------------------------------
                    # REGRA FUNDAMENTAL
                    #
                    # id > sinal_base_id
                    #
                    # Portanto a rodada que gerou o sinal NÃO pode
                    # ser usada como resultado.
                    #
                    # E também não usamos >=.
                    # ------------------------------------------------

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
                    """, (
                        sinal_base_id,
                    ))

                    resultado = cur.fetchone()


                    # ------------------------------------------------
                    # AINDA NÃO CHEGOU A PRÓXIMA RODADA
                    # ------------------------------------------------

                    if not resultado:

                        conn.close()

                        time.sleep(2)

                        continue


                    (
                        resultado_id,
                        rodada_resultado,
                        cor_resultado,
                        color_resultado,
                        roll_resultado
                    ) = resultado


                    # ------------------------------------------------
                    # PROTEÇÃO EXTRA
                    #
                    # Mesmo que exista algum problema de ordenação,
                    # nunca aceitamos a mesma rodada da base.
                    # ------------------------------------------------

                    if resultado_id <= sinal_base_id:

                        print(
                            "⚠️ Resultado rejeitado: "
                            "ID não é posterior à base.",
                            flush=True
                        )

                        conn.close()

                        time.sleep(2)

                        continue


                    if rodada_resultado == ultima_rodada_sinal:

                        print(
                            "⚠️ Resultado rejeitado: "
                            "rodada igual à base.",
                            flush=True
                        )

                        conn.close()

                        time.sleep(2)

                        continue


                    # ------------------------------------------------
                    # COR REAL
                    # ------------------------------------------------

                    cor_real = obter_cor(
                        cor_resultado,
                        color_resultado
                    )


                    # =================================================
                    # BRANCO
                    #
                    # NÃO CONTABILIZA.
                    #
                    # Não é WIN.
                    # Não é LOSS.
                    # Não incrementa whites.
                    # Não entra na taxa.
                    #
                    # E o sinal é encerrado para impedir que a mesma
                    # previsão fique aguardando indefinidamente.
                    # =================================================

                    if cor_real == "W":

                        print(
                            f"⚪ BRANCO IGNORADO | "
                            f"Base={ultima_rodada_sinal} | "
                            f"Resultado={rodada_resultado} | "
                            f"Roll={roll_resultado}",
                            flush=True
                        )

                        cur.execute("""
                            UPDATE bot_estado

                            SET
                                sinal_ativo = FALSE,

                                cor_sinal = NULL,

                                ultima_estrategia = NULL,

                                ultima_rodada_sinal = NULL,

                                sinal_base_id = NULL,

                                atualizado_em =
                                    CURRENT_TIMESTAMP

                            WHERE id = 1;
                        """)

                        conn.commit()

                        conn.close()

                        time.sleep(1)

                        continue


                    # ------------------------------------------------
                    # COR INVÁLIDA
                    # ------------------------------------------------

                    if cor_real is None:

                        print(
                            "⚠️ Não foi possível determinar "
                            "a cor do resultado.",
                            flush=True
                        )

                        conn.close()

                        time.sleep(2)

                        continue


                    # =================================================
                    # DETERMINAR WIN / LOSS
                    # =================================================

                    if cor_real == cor_sinal:

                        resultado_status = "WIN"

                    else:

                        resultado_status = "LOSS"


                    print(
                        f"🎯 SINAL RESOLVIDO | "
                        f"Estratégia={ultima_estrategia} | "
                        f"Previsão={cor_sinal} | "
                        f"Resultado={cor_real} | "
                        f"Base={ultima_rodada_sinal} | "
                        f"Rodada={rodada_resultado} | "
                        f"Status={resultado_status}",
                        flush=True
                    )


                    # =================================================
                    # REGISTRA SINAL
                    #
                    # Branco NÃO chega aqui.
                    # =================================================

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


                    # =================================================
                    # ATUALIZA PLACAR
                    #
                    # Somente R/P chegam aqui.
                    # =================================================

                    if resultado_status == "WIN":

                        cur.execute("""
                            UPDATE bot_estado

                            SET
                                wins = wins + 1,

                                profit = profit + 1.0,

                                sinal_ativo = FALSE,

                                cor_sinal = NULL,

                                ultima_estrategia = NULL,

                                ultima_rodada_sinal = NULL,

                                sinal_base_id = NULL,

                                atualizado_em =
                                    CURRENT_TIMESTAMP

                            WHERE id = 1;
                        """)


                    else:

                        cur.execute("""
                            UPDATE bot_estado

                            SET
                                losses = losses + 1,

                                profit = profit - 1.0,

                                sinal_ativo = FALSE,

                                cor_sinal = NULL,

                                ultima_estrategia = NULL,

                                ultima_rodada_sinal = NULL,

                                sinal_base_id = NULL,

                                atualizado_em =
                                    CURRENT_TIMESTAMP

                            WHERE id = 1;
                        """)


                    conn.commit()

                    conn.close()

                    time.sleep(1)

                    continue


                # =================================================
                # 2. PROCURAR NOVO GATILHO
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

                    LIMIT 10;
                """)

                rows = cur.fetchall()


                if not rows:

                    conn.close()

                    time.sleep(2)

                    continue


                # Ordem cronológica
                rows = list(reversed(rows))


                # ------------------------------------------------
                # MONTAR HISTÓRICO
                # ------------------------------------------------

                hist = []

                for row in rows:

                    item = row_para_hist(row)

                    if item["cor"] is not None:

                        hist.append(item)


                if not hist:

                    conn.close()

                    time.sleep(2)

                    continue


                # =================================================
                # DETECTAR ESTRATÉGIA
                # =================================================

                cor_prevista, estrategia = (
                    detectar_estrategia(hist)
                )


                if not cor_prevista:

                    conn.close()

                    time.sleep(2)

                    continue


                # =================================================
                # RODADA BASE
                #
                # A última rodada do padrão é a BASE.
                #
                # Exemplo:
                #
                # R R
                #     ↑
                #     base
                #
                # O próximo evento será o resultado.
                # =================================================

                rodada_base = hist[-1]

                id_base = rodada_base["id"]

                rodada_id_base = (
                    rodada_base["rodada_id"]
                )


                # =================================================
                # PROTEÇÃO CONTRA REPETIÇÃO
                # =================================================

                if (
                    ultima_rodada_sinal
                    == rodada_id_base
                ):

                    conn.close()

                    time.sleep(2)

                    continue


                # =================================================
                # CRIAR SINAL
                # =================================================

                print(
                    f"📊 NOVO SINAL | "
                    f"Estratégia={estrategia} | "
                    f"Base={rodada_id_base} | "
                    f"Entrada={cor_prevista}",
                    flush=True
                )


                cur.execute("""
                    UPDATE bot_estado

                    SET
                        sinal_ativo = TRUE,

                        cor_sinal = %s,

                        ultima_estrategia = %s,

                        ultima_rodada_sinal = %s,

                        sinal_base_id = %s,

                        atualizado_em =
                            CURRENT_TIMESTAMP

                    WHERE id = 1;
                """, (
                    cor_prevista,
                    estrategia,
                    rodada_id_base,
                    id_base
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


        time.sleep(2)


# ================================================================
# INFORMAÇÕES DAS CORES PARA O DASHBOARD
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
            "sigla": "P",
            "nome": "PRETO",
            "classe": "black",
            "emoji": "⚫"
        }


    texto = (
        cor_texto or ""
    ).upper()


    if (
        "VERMELHO" in texto
        or texto == "RED"
        or texto == "R"
        or texto == "V"
        or texto == "VI"
    ):

        return {
            "sigla": "R",
            "nome": "VERMELHO",
            "classe": "red",
            "emoji": "🔴"
        }


    if (
        "PRETO" in texto
        or texto == "BLACK"
        or texto == "P"
        or texto == "B"
    ):

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

    mapa = {

        "VI → VI → R":
            "🥇 VI → VI → R",

        "VI → PP → P":
            "🥈 VI → PP → P",

        "VI → VI → VI → R":
            "🥉 VI → VI → VI → R",

        "W + 13":
            "⚪ W + 13"
    }

    return mapa.get(
        nome,
        nome
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

            # Mantido somente por compatibilidade.
            # Não será incrementado.
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

            # ====================================================
            # STATUS COLLECTOR
            # ====================================================

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


            # ====================================================
            # ESTADO BOT
            # ====================================================

            cur.execute("""
                SELECT
                    motor_ativo,
                    sinal_ativo,
                    cor_sinal,
                    ultima_estrategia,
                    wins,
                    losses,
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
                    profit,
                    inicio_sessao
                ) = estado


                vazio["motor"] = {

                    "ativo":
                        bool(motor_ativo),

                    "sinal":
                        bool(sinal_ativo),

                    "cor":
                        cor_sinal,

                    "estrategia":
                        estrategia_curta(
                            ultima_estrategia
                        )
                        if ultima_estrategia
                        else None,

                    "wins":
                        wins or 0,

                    "losses":
                        losses or 0,

                    # Branco deliberadamente não contabilizado
                    "whites":
                        0,

                    "profit":
                        float(
                            profit or 0
                        )
                }


            # ====================================================
            # HISTÓRICO DA SESSÃO
            # ====================================================

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
                """, (
                    inicio_sessao,
                ))

            else:

                cur.execute("""
                    SELECT
                        roll,
                        color,
                        cor,
                        rodada_id

                    FROM blaze_historico

                    WHERE FALSE;
                """)


            rows = list(
                reversed(
                    cur.fetchall()
                )
            )


            vazio["total_jogos"] = len(rows)


            for (
                roll,
                color,
                cor,
                rodada_id
            ) in rows:

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


            # ====================================================
            # HISTÓRICO DE SINAIS
            #
            # IMPORTANTE:
            #
            # Aqui não existe WHITE.
            # Branco nunca é inserido em estrategia_sinais.
            # ====================================================

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
                """, (
                    inicio_sessao,
                ))


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


                    vazio[
                        "historico_sinais"
                    ].append({

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

                Branco não contabilizado

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

                O sinal será resolvido
                somente pela próxima rodada
                posterior à base.

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

        Blaze Bot • coleta contínua •
        motor estatístico independente •
        branco não contabilizado •
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

    return render_template_string(
        HTML,
        status=consultar_dashboard()
    )


# ================================================================
# CONTROLE DO MOTOR
#
# ATENÇÃO:
#
# Isto NÃO controla o collector.
#
# O collector continua funcionando.
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


            motor_ativo = bool(
                estado[0]
            )


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

                        atualizado_em =
                            CURRENT_TIMESTAMP

                    WHERE id = 1;
                """)


            # ====================================================
            # INICIAR NOVA SESSÃO
            # ====================================================

            else:

                print(
                    "▶️ Iniciando NOVA SESSÃO.",
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

                        inicio_sessao =
                            CURRENT_TIMESTAMP,

                        ultima_rodada_sinal = NULL,

                        sinal_base_id = NULL,

                        atualizado_em =
                            CURRENT_TIMESTAMP

                    WHERE id = 1;
                """)


        conn.commit()


    except Exception as e:

        print(
            f"❌ Erro ao alterar estado "
            f"do motor: {e}",
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
#
# IMPORTANTE:
#
# O collector é independente do motor.
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
            f"❌ Não foi possível iniciar "
            f"o collector: {e}",
            flush=True
        )


# ================================================================
# INICIAR MOTOR
# ================================================================

def iniciar_background_motor():

    print(
        "🧠 Iniciando Motor Estatístico "
        "em background...",
        flush=True
    )


    motor_de_padroes()


# ================================================================
# INICIALIZAÇÃO DO BANCO
# ================================================================

init_web_db()


# ================================================================
# THREAD DO COLLECTOR
#
# NÃO depende de motor_ativo.
#
# Portanto:
#
# Motor parado
#      ↓
# Collector continua
#      ↓
# Jogos continuam indo para o banco
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
