# ================================================================
# BLAZE — ESTRATÉGIA 2 ADAPTADA PELA ESTRATÉGIA 3
# ================================================================
#
# ESTRATÉGIA BASE:
#   Estratégia 2 = gatilho após BRANCO
#
# DECISÃO:
#   Estratégia 3 = Random Forest usando os últimos 5 resultados
#
# REGRAS:
#   1. BRANCO continua fazendo parte do histórico.
#   2. BRANCO funciona como gatilho para procurar entrada.
#   3. O modelo utiliza os últimos 5 resultados.
#   4. Só gera sinal se a confiança >= 55%.
#   5. Sinal pode ser somente VERMELHO ou PRETO.
#   6. BRANCO após VERMELHO/PRETO = LOSS.
#   7. Cada sinal é avaliado somente na próxima rodada.
#   8. Não existe GALE.
#   9. Não existe empate.
#
# ================================================================

import pandas as pd
import numpy as np
import time
import gspread

from google.colab import auth
from google.auth import default
from IPython.display import clear_output

from sklearn.ensemble import RandomForestClassifier


# ================================================================
# CONFIGURAÇÕES
# ================================================================

NOME_DA_PLANILHA = "Blaze Bot"
NOME_DA_ABA = "blaze_historico"

# Confiança mínima para gerar sinal
LIMIAR_CONFIANCA = 0.55

# Quantidade de resultados usados pelo modelo
JANELA_HISTORICO = 5

# Quantidade mínima de dados para treinar
MINIMO_TREINO = 50

# Intervalo de atualização da planilha
INTERVALO_ATUALIZACAO = 5


# ================================================================
# MAPEAMENTO
# ================================================================

# Mantemos BRANCO dentro do histórico.
#
# VERMELHO = 0
# PRETO    = 1
# BRANCO   = 2
#
MAPEAMENTO_COR = {
    "VERMELHO": 0,
    "PRETO": 1,
    "BRANCO": 2,
    "V": 0,
    "P": 1,
    "B": 2
}


# ================================================================
# CONEXÃO COM GOOGLE SHEETS
# ================================================================

def conectar_planilha():

    print("🔐 Conectando ao Google Sheets...")

    auth.authenticate_user()

    creds, _ = default()

    gc = gspread.authorize(creds)

    planilha = gc.open(NOME_DA_PLANILHA)

    aba = planilha.worksheet(NOME_DA_ABA)

    print("✅ Conexão estabelecida.")

    return aba


# ================================================================
# NORMALIZAÇÃO DAS CORES
# ================================================================

def normalizar_cor(valor):

    if pd.isna(valor):
        return None

    texto = str(valor).upper().strip()

    # Remove possíveis espaços
    texto = texto.replace(" ", "")

    if texto in ["VERMELHO", "V", "RED"]:
        return "VERMELHO"

    if texto in ["PRETO", "P", "BLACK"]:
        return "PRETO"

    if texto in ["BRANCO", "B", "WHITE"]:
        return "BRANCO"

    return None


# ================================================================
# PREPARAR HISTÓRICO
# ================================================================

def preparar_historico(df):

    if df.empty:
        return df

    # ------------------------------------------------------------
    # Identificar coluna de cor
    # ------------------------------------------------------------

    if "cor" in df.columns:

        coluna_cor = "cor"

    else:

        # Procura alguma coluna compatível
        coluna_cor = None

        for coluna in df.columns:

            nome = str(coluna).lower()

            if nome in ["color", "colour", "cor"]:

                coluna_cor = coluna
                break

        if coluna_cor is None:

            raise ValueError(
                "Não foi encontrada uma coluna de cor na planilha."
            )

    # ------------------------------------------------------------
    # Normalizar cor
    # ------------------------------------------------------------

    df["cor_normalizada"] = df[coluna_cor].apply(normalizar_cor)

    # Remove registros sem cor válida
    df = df[df["cor_normalizada"].notna()].copy()

    # ------------------------------------------------------------
    # Ordenação cronológica
    # ------------------------------------------------------------

    if "created_at" in df.columns:

        df["created_at_dt"] = pd.to_datetime(
            df["created_at"],
            errors="coerce"
        )

        if df["created_at_dt"].notna().any():

            df = df.sort_values(
                "created_at_dt"
            ).reset_index(drop=True)

    elif "timestamp" in df.columns:

        df["timestamp_dt"] = pd.to_datetime(
            df["timestamp"],
            errors="coerce"
        )

        if df["timestamp_dt"].notna().any():

            df = df.sort_values(
                "timestamp_dt"
            ).reset_index(drop=True)

    else:

        df = df.reset_index(drop=True)

    return df


# ================================================================
# CONVERTER HISTÓRICO PARA NÚMEROS
# ================================================================

def obter_valores_numericos(df):

    valores = []

    for cor in df["cor_normalizada"]:

        valores.append(
            MAPEAMENTO_COR[cor]
        )

    return np.array(valores)


# ================================================================
# CRIAR DATASET PARA O RANDOM FOREST
# ================================================================

def criar_dataset_treino(valores):

    X = []
    y = []

    #
    # Utilizamos os últimos 5 resultados
    # para prever o próximo resultado.
    #
    # Exemplo:
    #
    # [V, P, B, P, V] -> próximo resultado
    #

    for i in range(
        JANELA_HISTORICO,
        len(valores)
    ):

        janela = valores[
            i - JANELA_HISTORICO:i
        ]

        resultado = valores[i]

        #
        # BRANCO não é uma classe de previsão.
        #
        # Quando o próximo resultado é BRANCO,
        # ele não entra como alvo de treinamento.
        #
        if resultado not in [0, 1]:
            continue

        X.append(janela)

        y.append(resultado)

    if len(X) == 0:

        return (
            np.empty((0, JANELA_HISTORICO)),
            np.array([])
        )

    return np.array(X), np.array(y)


# ================================================================
# TREINAR MODELO
# ================================================================

def treinar_modelo(valores):

    X, y = criar_dataset_treino(valores)

    if len(X) < MINIMO_TREINO:

        return None, X, y

    #
    # É necessário ter as duas classes:
    # VERMELHO e PRETO
    #

    classes_unicas = np.unique(y)

    if len(classes_unicas) < 2:

        return None, X, y

    modelo = RandomForestClassifier(

        n_estimators=100,

        max_depth=6,

        min_samples_split=20,

        random_state=42

    )

    modelo.fit(X, y)

    return modelo, X, y


# ================================================================
# GERAR SINAL
# ================================================================

def gerar_sinal(modelo, ultimos_resultados):

    if modelo is None:

        return None, 0.0, 0.0

    if len(ultimos_resultados) < JANELA_HISTORICO:

        return None, 0.0, 0.0

    #
    # Últimos 5 resultados
    #

    features = np.array(
        ultimos_resultados[-JANELA_HISTORICO:]
    ).reshape(
        1,
        -1
    )

    probabilidades = modelo.predict_proba(
        features
    )[0]

    #
    # Como o modelo pode ter as classes em ordem
    # diferente, usamos modelo.classes_
    #

    prob_vermelho = 0.0
    prob_preto = 0.0

    for classe, prob in zip(
        modelo.classes_,
        probabilidades
    ):

        if classe == 0:

            prob_vermelho = float(prob)

        elif classe == 1:

            prob_preto = float(prob)

    #
    # Escolha somente se atingir o limiar
    #

    if prob_vermelho >= LIMIAR_CONFIANCA:

        return (
            "VERMELHO",
            prob_vermelho,
            prob_preto
        )

    if prob_preto >= LIMIAR_CONFIANCA:

        return (
            "PRETO",
            prob_vermelho,
            prob_preto
        )

    return (
        None,
        prob_vermelho,
        prob_preto
    )


# ================================================================
# AVALIAR SINAL
# ================================================================

def avaliar_sinal(sinal, resultado):

    if sinal is None:

        return None

    #
    # IMPORTANTE:
    #
    # Branco NÃO é empate.
    #
    # Se sinal = VERMELHO e resultado = BRANCO:
    # LOSS
    #
    # Se sinal = PRETO e resultado = BRANCO:
    # LOSS
    #

    if resultado == sinal:

        return "WIN"

    return "LOSS"


# ================================================================
# FORMATAÇÃO
# ================================================================

def nome_curto(cor):

    if cor == "VERMELHO":
        return "🔴 V"

    if cor == "PRETO":
        return "⚫ P"

    if cor == "BRANCO":
        return "⚪ B"

    return "?"


# ================================================================
# MONITOR PRINCIPAL
# ================================================================

def monitorar_estrategia_2_adaptada():

    aba = conectar_planilha()

    ultimo_tamanho_df = 0

    #
    # Histórico dos sinais já avaliados
    #

    sinais_historico = []

    wins = 0
    losses = 0

    max_seq_wins = 0
    max_seq_losses = 0

    seq_wins = 0
    seq_losses = 0

    #
    # Sinal que aguarda o próximo resultado
    #

    pendente_sinal = None

    #
    # Controle do último resultado processado
    #

    ultimo_registro_processado = None

    print()
    print("=" * 75)
    print("🚀 ESTRATÉGIA 2 ADAPTADA PELA ESTRATÉGIA 3")
    print("=" * 75)
    print()
    print("🎯 Gatilho: BRANCO")
    print("🧠 Modelo: Random Forest")
    print(f"📊 Janela: {JANELA_HISTORICO} resultados")
    print(f"🎚️ Confiança mínima: {LIMIAR_CONFIANCA:.0%}")
    print("⚪ BRANCO no histórico: SIM")
    print("⚪ BRANCO após sinal: LOSS")
    print()

    while True:

        try:

            # ====================================================
            # LER PLANILHA
            # ====================================================

            dados = aba.get_all_records()

            df = pd.DataFrame(dados)

            if df.empty:

                time.sleep(
                    INTERVALO_ATUALIZACAO
                )

                continue

            # ====================================================
            # PREPARAR DADOS
            # ====================================================

            df = preparar_historico(df)

            if df.empty:

                time.sleep(
                    INTERVALO_ATUALIZACAO
                )

                continue

            valores = obter_valores_numericos(df)

            tamanho_atual = len(df)

            #
            # Se não houve rodada nova,
            # apenas atualizamos o painel.
            #

            houve_novidade = (
                tamanho_atual >
                ultimo_tamanho_df
            )

            if houve_novidade:

                # =================================================
                # PROCESSAR SOMENTE NOVOS RESULTADOS
                # =================================================

                if ultimo_tamanho_df == 0:

                    inicio_processamento = 0

                else:

                    inicio_processamento = ultimo_tamanho_df

                novos_indices = range(
                    inicio_processamento,
                    tamanho_atual
                )

                #
                # Treinar modelo com histórico disponível
                #

                modelo, X, y = treinar_modelo(
                    valores
                )

                for indice in novos_indices:

                    cor_atual = (
                        df.iloc[indice]
                        ["cor_normalizada"]
                    )

                    # =============================================
                    # PRIMEIRO:
                    # RESOLVER SINAL PENDENTE
                    # =============================================

                    if pendente_sinal is not None:

                        resultado = avaliar_sinal(
                            pendente_sinal["sinal"],
                            cor_atual
                        )

                        if resultado is not None:

                            #
                            # Registrar
                            #

                            registro = {

                                "sinal":
                                    pendente_sinal[
                                        "sinal"
                                    ],

                                "confianca":
                                    pendente_sinal[
                                        "confianca"
                                    ],

                                "resultado":
                                    cor_atual,

                                "status":
                                    resultado
                            }

                            sinais_historico.append(
                                registro
                            )

                            # -------------------------------------
                            # WIN
                            # -------------------------------------

                            if resultado == "WIN":

                                wins += 1

                                seq_wins += 1
                                seq_losses = 0

                                if seq_wins > max_seq_wins:

                                    max_seq_wins = seq_wins

                            # -------------------------------------
                            # LOSS
                            # -------------------------------------

                            else:

                                losses += 1

                                seq_losses += 1
                                seq_wins = 0

                                if seq_losses > max_seq_losses:

                                    max_seq_losses = seq_losses

                            #
                            # Sinal foi resolvido
                            #

                            pendente_sinal = None

                    # =============================================
                    # SEGUNDO:
                    # DETECTAR BRANCO
                    # =============================================

                    #
                    # IMPORTANTE:
                    #
                    # O Branco é o GATILHO.
                    #
                    # Mas ele continua dentro do histórico.
                    #

                    if cor_atual == "BRANCO":

                        #
                        # Só procurar uma nova entrada
                        # se não existe sinal pendente.
                        #

                        if pendente_sinal is None:

                            #
                            # Precisamos ter 5 resultados
                            # anteriores ao Branco.
                            #

                            if indice >= JANELA_HISTORICO:

                                historico_ate_agora = (
                                    valores[:indice]
                                )

                                #
                                # Treinar novamente utilizando
                                # tudo que existia antes do gatilho.
                                #
                                # Isso evita usar o próprio Branco
                                # como resultado futuro.
                                #

                                modelo_gatilho, Xg, yg = (
                                    treinar_modelo(
                                        historico_ate_agora
                                    )
                                )

                                if modelo_gatilho is not None:

                                    ultimos_5 = (
                                        historico_ate_agora[
                                            -JANELA_HISTORICO:
                                        ]
                                    )

                                    sinal, prob_v, prob_p = (
                                        gerar_sinal(
                                            modelo_gatilho,
                                            ultimos_5
                                        )
                                    )

                                    if sinal is not None:

                                        if sinal == "VERMELHO":

                                            confianca = prob_v

                                        else:

                                            confianca = prob_p

                                        pendente_sinal = {

                                            "sinal":
                                                sinal,

                                            "confianca":
                                                confianca,

                                            "prob_vermelho":
                                                prob_v,

                                            "prob_preto":
                                                prob_p,

                                            "gatilho_indice":
                                                indice
                                        }

                    # =================================================
                    # FIM DO PROCESSAMENTO
                    # =================================================

                #
                # Atualizar tamanho processado
                #

                ultimo_tamanho_df = tamanho_atual

            # ====================================================
            # PAINEL
            # ====================================================

            clear_output(
                wait=True
            )

            total = wins + losses

            if total > 0:

                assertividade = (
                    wins /
                    total *
                    100
                )

            else:

                assertividade = 0.0

            saldo = wins - losses

            print("=" * 75)
            print(
                "🎯 BLAZE — ESTRATÉGIA 2 + RANDOM FOREST"
            )
            print("=" * 75)

            print()

            print(
                f"📊 REGISTROS NA BASE: "
                f"{tamanho_atual}"
            )

            print(
                f"🎯 ENTRADAS AVALIADAS: "
                f"{total}"
            )

            print()

            print(
                f"🟢 WINS: "
                f"{wins}"
            )

            print(
                f"🔴 LOSSES: "
                f"{losses}"
            )

            print(
                f"📈 ASSERTIVIDADE: "
                f"{assertividade:.2f}%"
            )

            print(
                f"💰 SALDO: "
                f"{saldo:+d}"
            )

            print()

            print(
                f"🔥 MAIOR SEQUÊNCIA WIN: "
                f"{max_seq_wins}"
            )

            print(
                f"❄️ MAIOR SEQUÊNCIA LOSS: "
                f"{max_seq_losses}"
            )

            print()
            print("-" * 75)

            # ====================================================
            # ÚLTIMO SINAL RESOLVIDO
            # ====================================================

            if len(sinais_historico) > 0:

                ultimo = sinais_historico[-1]

                print(
                    "📌 ÚLTIMO SINAL RESOLVIDO"
                )

                print(
                    f"   Sinal: "
                    f"{ultimo['sinal']}"
                )

                print(
                    f"   Confiança: "
                    f"{ultimo['confianca']:.2%}"
                )

                print(
                    f"   Resultado: "
                    f"{ultimo['resultado']}"
                )

                if ultimo["status"] == "WIN":

                    print(
                        "   Status: ✅ WIN"
                    )

                else:

                    print(
                        "   Status: ❌ LOSS"
                    )

                print()

            # ====================================================
            # SINAL PENDENTE
            # ====================================================

            if pendente_sinal is not None:

                print(
                    "🚨 ENTRADA ATIVA"
                )

                print(
                    f"   🎯 Apostar em: "
                    f"{pendente_sinal['sinal']}"
                )

                print(
                    f"   🧠 Confiança: "
                    f"{pendente_sinal['confianca']:.2%}"
                )

                print(
                    f"   🔴 VERMELHO: "
                    f"{pendente_sinal['prob_vermelho']:.2%}"
                )

                print(
                    f"   ⚫ PRETO: "
                    f"{pendente_sinal['prob_preto']:.2%}"
                )

                print(
                    "   ⏳ Aguardando próxima rodada..."
                )

            else:

                print(
                    "⏳ NENHUMA ENTRADA ATIVA"
                )

                print(
                    "   Aguardando BRANCO para "
                    "ativar o gatilho."
                )

            print()
            print("-" * 75)

            # ====================================================
            # ÚLTIMOS RESULTADOS
            # ====================================================

            ultimos_exibicao = (
                df["cor_normalizada"]
                .tail(15)
                .tolist()
            )

            print(
                "📜 ÚLTIMOS 15 RESULTADOS:"
            )

            print(
                " ".join(
                    nome_curto(cor)
                    for cor in ultimos_exibicao
                )
            )

            print()
            print("=" * 75)

            if modelo is None:

                print(
                    "⚠️ Modelo ainda sem dados suficientes "
                    "para treinamento."
                )

            else:

                print(
                    f"🧠 Modelo treinado com "
                    f"{len(X)} exemplos."
                )

            print(
                f"🔄 Atualização a cada "
                f"{INTERVALO_ATUALIZACAO}s"
            )

            print("=" * 75)

            time.sleep(
                INTERVALO_ATUALIZACAO
            )

        except Exception as e:

            print()
            print("=" * 75)
            print("⚠️ ERRO NO MONITOR")
            print("=" * 75)
            print(
                f"{type(e).__name__}: {e}"
            )
            print()
            print(
                "🔄 Tentando novamente em "
                f"{INTERVALO_ATUALIZACAO}s..."
            )
            print("=" * 75)

            time.sleep(
                INTERVALO_ATUALIZACAO
            )


# ================================================================
# INICIAR
# ================================================================

monitorar_estrategia_2_adaptada()
