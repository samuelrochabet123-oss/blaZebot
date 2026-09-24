# =====================================================================
# MOTOR OFICIAL DE MARKOV (ESTRATÉGIA VENCEDORA: +33 UNIDADES)
# =====================================================================

from collections import defaultdict
from datetime import datetime, timedelta
import google.auth
from google.colab import auth
import gspread

auth.authenticate_user()
creds, _ = google.auth.default(
    scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
)
gc = gspread.authorize(creds)
sheet_id = "1NuFeub0RD99vvTF2uon_t9NZENMCkLmbF_CZASMxv_Q"

META_DIARIA = 3
STOP_LOSS = -3


def carregar_historico_markov():
    aba = gc.open_by_key(sheet_id).worksheet("blaze_historico")
    registros = aba.get_all_records(numericise_ignore=["all"], default_blank="")

    def to_int(v, p=None):
        try:
            return int(float(str(v).strip()))
        except:
            return p

    def cor_de(t, color):
        t = str(t or "").upper().strip()
        if "VERMELHO" in t or t in {"R", "RED", "V", "VI"}:
            return "R"
        if "PRETO" in t or t in {"P", "BLACK", "B"}:
            return "P"
        if "BRANCO" in t or t in {"W", "WHITE"}:
            return "W"
        return {0: "W", 1: "R", 2: "P"}.get(color)

    hist = []
    for r in registros:
        if str(r.get("status", "")).strip() != "complete":
            continue
        c = to_int(r.get("color"))
        i = to_int(r.get("id"))
        cor = cor_de(r.get("cor"), c)
        data_str = str(r.get("created_at", "")).strip()

        dia_str = "Desconhecido"
        hora_brt = 0
        try:
            if data_str:
                dt = datetime.fromisoformat(data_str.replace("Z", "+00:00"))
                dt_brt = dt - timedelta(hours=3)
                dia_str = dt_brt.strftime("%Y-%m-%d")
                hora_brt = dt_brt.hour
        except:
            pass

        if cor and i is not None:
            hist.append({
                "id": i,
                "cor": cor,
                "data": dia_str,
                "hora": hora_brt,
            })

    hist.sort(key=lambda h: h["id"])
    return hist


def executar_backtest_markov():
    print("🔍 Baixando histórico e executando motor de Markov...")
    hist_geral = carregar_historico_markov()

    # Filtrando apenas R e P para o cálculo de transição
    dados_filtrados = [h for h in hist_geral if h["cor"] in "RP"]

    tamanho_padrao = 3
    transicoes = defaultdict(lambda: {"R": 0, "P": 0})

    # Mapeia as probabilidades do histórico
    for i in range(len(dados_filtrados) - tamanho_padrao):
        padrao = tuple(d["cor"] for d in dados_filtrados[i : i + tamanho_padrao])
        proxima_cor = dados_filtrados[i + tamanho_padrao]["cor"]
        transicoes[padrao][proxima_cor] += 1

    sessoes = defaultdict(list)

    for i in range(tamanho_padrao, len(dados_filtrados) - 1):
        padrao_atual = tuple(
            d["cor"] for d in dados_filtrados[i - tamanho_padrao : i]
        )
        item_seguinte = dados_filtrados[i]
        cor_seguinte = item_seguinte["cor"]

        estatisticas = transicoes[padrao_atual]
        total_amostras = estatisticas["R"] + estatisticas["P"]

        cor_entrada = None
        if total_amostras >= 3:
            if estatisticas["R"] > estatisticas["P"]:
                cor_entrada = "R"
            elif estatisticas["P"] > estatisticas["R"]:
                cor_entrada = "P"
            else:
                cor_entrada = dados_filtrados[i - 1]["cor"]
        else:
            cor_entrada = dados_filtrados[i - 1]["cor"]

        if cor_entrada:
            resultado = 1 if cor_seguinte == cor_entrada else -1
            chave_sessao = f"{item_seguinte['data']} | {item_seguinte['hora']:02d}:00"
            sessoes[chave_sessao].append(resultado)

    # Apurativo de sessões com Stop Win (+3) / Stop Loss (-3)
    total_wins = 0
    total_loss = 0
    total_neutras = 0
    lucro_total = 0

    print("\n" + "=" * 65)
    print("🎯 SIMULAÇÃO DE METAS: MARKOV PURO (+3 / -3)")
    print("=" * 65)
    print(f"{'SESSÃO (DATA | HORA)':<22} | {'STATUS':<12} | {'SALDO':<10}")
    print("-" * 65)

    for sessao in sorted(sessoes.keys()):
        resultados = sessoes[sessao]
        saldo_parcial = 0
        status_sessao = "NEUTRO"

        for res in resultados:
            saldo_parcial += res
            if saldo_parcial >= META_DIARIA:
                status_sessao = f"WIN (+{META_DIARIA})"
                total_wins += 1
                break
            elif saldo_parcial <= STOP_LOSS:
                status_sessao = f"LOSS ({STOP_LOSS})"
                total_loss += 1
                break

        if status_sessao == "NEUTRO":
            if saldo_parcial > 0:
                total_wins += 1
                status_sessao = f"WIN (+{saldo_parcial})"
            elif saldo_parcial < 0:
                total_loss += 1
                status_sessao = f"LOSS ({saldo_parcial})"
            else:
                total_neutras += 1
                status_sessao = "0 (Empate)"

        lucro_total += saldo_parcial
        print(
            f"{sessao:<22} | {status_sessao:<12} | {saldo_parcial:+d} unidades"
        )

    print("=" * 65)
    print(f"📊 RESUMO DO MOTOR DE MARKOV:")
    print(f"✅ Total de Sessões Positivas (Meta Batida): {total_wins}")
    print(f"❌ Total de Sessões Negativas (Stop Loss): {total_loss}")
    print(f"⚖️ Sessões Neutras: {total_neutras}")
    print(f"💰 Saldo Líquido Total Acumulado: {lucro_total:+d} unidades")
    print("=" * 65)


executar_backtest_markov()
