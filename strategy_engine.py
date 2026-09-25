# =====================================================================
# BLAZE STRATEGY ENGINE — MULTI-GATILHOS PÓS-BRANCO
# Estratégia baseada exatamente no script de referência enviado pelo usuário.
#
# REGRA:
# 1) Localiza o último BRANCO.
# 2) Espera exatamente 3 resultados após esse BRANCO.
# 3) Forma o padrão de 3 cores (R/P/W).
# 4) Procura TODAS as ocorrências desse padrão na base histórica.
# 5) Só cria gatilho se o padrão tiver pelo menos 3 ocorrências.
# 6) Prediz R ou P pela maior frequência histórica do próximo resultado.
# 7) Empate R/P = sem sinal.
# 8) O sinal aponta exclusivamente para a PRÓXIMA rodada (+1).
# 9) BRANCO no alvo = LOSS técnico, como no script de referência.
#
# Importante:
# - O mapeamento é recalculado a cada rodada usando o histórico disponível.
# - Este motor NÃO usa ELITE, QUEBRA_ALT ou as regras antigas de pós-branco.
# - A coluna "estrategia_sinais" continua sendo usada pelo dashboard.
# =====================================================================

import os
from datetime import datetime
import requests
import sheets_db as db

APOSTA_BASE = 1.0
RESULTADO_PENDENTE = "PENDENTE"
RESULTADO_WIN = "WIN"
RESULTADO_LOSS = "LOSS"

PREFIXO_ESTRATEGIA = "PÓS-BRANCO — MULTI-GATILHOS"
TAMANHO_JANELA = 3
MIN_OCORRENCIAS = 3

NOMES = {
    "R": "VERMELHO",
    "P": "PRETO",
    "W": "BRANCO",
}

_iniciado = False


# ---------------------------------------------------------------------
# UTILITÁRIOS
# ---------------------------------------------------------------------

def _nome(c):
    return NOMES.get(c, c or "?")


def _normalizar_cor(valor):
    if valor in ("R", "P", "W"):
        return valor

    try:
        v = str(valor).upper().strip()
    except Exception:
        return None

    if "VERMELHO" in v or v in {"R", "RED", "V", "VI"}:
        return "R"
    if "PRETO" in v or v in {"P", "BLACK", "B"}:
        return "P"
    if "BRANCO" in v or v in {"W", "WHITE"}:
        return "W"

    return None


def enviar_telegram(mensagem):
    token = os.getenv("TELEGRAM_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    if not token or not chat_id:
        print("⚠️ Telegram não configurado.", flush=True)
        return False

    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={
                "chat_id": chat_id,
                "text": mensagem,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        r.raise_for_status()
        j = r.json()

        if not j.get("ok"):
            print(f"❌ Telegram recusou: {j}", flush=True)
            return False

        print("📨 Telegram enviado.", flush=True)
        return True

    except Exception as e:
        print(f"❌ Erro Telegram: {e}", flush=True)
        return False


def init_engine_db():
    global _iniciado

    _iniciado = db.init_db()

    if _iniciado:
        print(
            "✅ Motor MULTI-GATILHOS PÓS-BRANCO inicializado.",
            flush=True,
        )
    else:
        print("❌ Falha inicializando motor.", flush=True)

    return _iniciado


# ---------------------------------------------------------------------
# 1. MAPEAMENTO DOS PADRÕES PÓS-BRANCO
# ---------------------------------------------------------------------

def construir_gatilhos_dinamicos(hist):
    """
    Reproduz a lógica do script de referência:

    Para cada BRANCO que tenha pelo menos 3 resultados depois dele:
        seq = próximas 3 cores
        próxima = cor seguinte à sequência

    Depois:
        - mantém padrões com total >= 3;
        - escolhe R se R > P;
        - escolhe P se P > R;
        - empate = nenhum gatilho.

    O W é contado no histórico, mas nunca é uma cor de entrada.
    """
    estatisticas = {}

    for idx, item in enumerate(hist):
        if item["cor"] != "W":
            continue

        # Precisamos de 3 cores para montar a sequência
        # e mais 1 resultado para ser o alvo histórico.
        if idx + TAMANHO_JANELA + 1 >= len(hist):
            continue

        seq = tuple(
            hist[idx + deslocamento]["cor"]
            for deslocamento in range(1, TAMANHO_JANELA + 1)
        )

        proxima = hist[idx + TAMANHO_JANELA + 1]["cor"]

        if seq not in estatisticas:
            estatisticas[seq] = {
                "R": 0,
                "P": 0,
                "W": 0,
                "total": 0,
            }

        estatisticas[seq][proxima] += 1
        estatisticas[seq]["total"] += 1

    gatilhos = {}

    for seq, dados in estatisticas.items():
        if dados["total"] < MIN_OCORRENCIAS:
            continue

        r_qtd = dados["R"]
        p_qtd = dados["P"]

        if r_qtd > p_qtd:
            gatilhos[seq] = {
                "cor": "R",
                "acertos": r_qtd,
                "total": dados["total"],
                "R": r_qtd,
                "P": p_qtd,
                "W": dados["W"],
            }

        elif p_qtd > r_qtd:
            gatilhos[seq] = {
                "cor": "P",
                "acertos": p_qtd,
                "total": dados["total"],
                "R": r_qtd,
                "P": p_qtd,
                "W": dados["W"],
            }

    return gatilhos, estatisticas


def detectar_sinal_pos_branco(hist):
    """
    Verifica se o histórico atual está exatamente 3 rodadas depois
    do último BRANCO.

    Exemplo:
        W -> R -> P -> R
                    ^ aqui nasce o sinal para a PRÓXIMA rodada
    """
    if len(hist) < 4:
        return None

    # Localiza o último branco.
    indices_brancos = [
        i for i, item in enumerate(hist)
        if item["cor"] == "W"
    ]

    if not indices_brancos:
        return None

    ultimo_idx_branco = indices_brancos[-1]
    distancia_fim = len(hist) - 1 - ultimo_idx_branco

    # O script de referência só gera sinal quando há exatamente
    # três resultados depois do último branco.
    if distancia_fim != TAMANHO_JANELA:
        return None

    seq = tuple(
        hist[ultimo_idx_branco + deslocamento]["cor"]
        for deslocamento in range(1, TAMANHO_JANELA + 1)
    )

    gatilhos, _ = construir_gatilhos_dinamicos(hist)

    regra = gatilhos.get(seq)
    if not regra:
        return None

    return {
        "estrategia": (
            f"{PREFIXO_ESTRATEGIA} — "
            f"[{' → '.join(seq)}] → {_nome(regra['cor'])}"
        ),
        "cor_entrada": regra["cor"],
        "cor_regra": regra["cor"],
        "alvo_offset": 1,
        "padrao": "".join(seq),
        "rodada_base": hist[-1]["rodada_id"],
        "acertos_hist": regra["acertos"],
        "total_hist": regra["total"],
        "R": regra["R"],
        "P": regra["P"],
        "W": regra["W"],
    }


# ---------------------------------------------------------------------
# BACKTEST INFORMATIVO
# ---------------------------------------------------------------------

def backtest_historico(hist):
    """
    Reproduz o backtest do script de referência.

    Atenção: assim como o script de referência, o mapeamento é construído
    com a base completa antes do loop. Portanto este backtest é descritivo
    e não é um backtest walk-forward sem vazamento temporal.
    """
    if not hist:
        return {
            "entradas": 0,
            "wins": 0,
            "losses": 0,
            "taxa": 0.0,
            "saldo": 0,
        }

    indices_brancos = [
        i for i, item in enumerate(hist)
        if item["cor"] == "W"
    ]

    gatilhos, _ = construir_gatilhos_dinamicos(hist)

    saldo = 0
    entradas = 0
    wins = 0
    losses = 0

    for idx in indices_brancos:
        if idx + 4 >= len(hist):
            continue

        seq = (
            hist[idx + 1]["cor"],
            hist[idx + 2]["cor"],
            hist[idx + 3]["cor"],
        )

        resultado_real = hist[idx + 4]["cor"]

        regra = gatilhos.get(seq)
        if not regra:
            continue

        # Igual ao script de referência:
        # W não entra no denominador como vitória/derrota física,
        # porque o backtest ignora W nessa etapa.
        if resultado_real != "W":
            entradas += 1

            if resultado_real == regra["cor"]:
                saldo += 1
                wins += 1
            else:
                saldo -= 1
                losses += 1

    taxa = (wins / entradas * 100) if entradas else 0.0

    return {
        "entradas": entradas,
        "wins": wins,
        "losses": losses,
        "taxa": taxa,
        "saldo": saldo,
    }


# ---------------------------------------------------------------------
# CRIAÇÃO DO SINAL
# ---------------------------------------------------------------------

def _criar_sinal(g, now):
    base = str(g["rodada_base"])
    estrategia = g["estrategia"]

    # Não cria duas vezes o mesmo gatilho na mesma rodada-base.
    if any(
        s["rodada_base"] == base
        and s["estrategia"] == estrategia
        for s in db.sinais_todos()
    ):
        return False

    cid = (
        f"POS3-{base}-"
        f"{int(datetime.now().timestamp() * 1000)}"
    )

    db.inserir_sinal(
        {
            "rodada_base": base,
            "estrategia": estrategia,
            "cor_prevista": g["cor_entrada"],
            "resultado": RESULTADO_PENDENTE,
            "tentativa": 1,
            "ciclo_id": cid,
            "cor_regra": g.get("cor_regra"),
            "cor_entrada": g["cor_entrada"],
            "valor_aposta": APOSTA_BASE,
            "alvo_offset": 1,
        }
    )

    print(
        "\n"
        "============================================================\n"
        "🎯 NOVO SINAL — MULTI-GATILHOS PÓS-BRANCO\n"
        "============================================================\n"
        f"📐 Padrão pós-branco : {g['padrao']}\n"
        f"📊 Histórico         : {g['acertos_hist']}/{g['total_hist']}\n"
        f"   R={g['R']} | P={g['P']} | W={g['W']}\n"
        f"👉 Entrada           : {_nome(g['cor_entrada'])}\n"
        f"🎲 Rodada base       : {base}\n"
        f"⏭️ Alvo              : próxima rodada (+1)\n"
        "============================================================",
        flush=True,
    )

    enviar_telegram(
        "🚨 *NOVO SINAL — MULTI-GATILHOS PÓS-BRANCO* 🚨\n\n"
        f"📐 Padrão: *{g['padrao']}*\n"
        f"📊 Histórico: *{g['acertos_hist']}/{g['total_hist']}*\n"
        f"🔴 R: {g['R']} | ⚫ P: {g['P']} | ⚪ W: {g['W']}\n"
        f"🎯 Entrada: *{_nome(g['cor_entrada'])}*\n"
        f"🎲 Rodada base: *{base}*\n"
        "⏭️ Alvo: *PRÓXIMA RODADA*\n"
        "💵 Valor: R$ 1,00"
    )

    return True


# ---------------------------------------------------------------------
# RESOLUÇÃO DOS SINAIS
# ---------------------------------------------------------------------

def _resolver_pendentes(atual_id, now):
    """
    Resolve apenas sinais criados por este novo motor.

    O alvo é:
        índice da rodada-base + alvo_offset

    Para esta estratégia, alvo_offset = 1.

    Branco no alvo é LOSS, porque somente a cor prevista R/P gera WIN.
    """
    hist = db.carregar_historico()

    pos = {h["id"]: i for i, h in enumerate(hist)}
    por_rodada = {
        str(h["rodada_id"]): h
        for h in hist
    }

    estado = db.ler_estado()
    inicio = db.txt(estado.get("inicio_sessao"))

    count = 0

    pendentes = db.sinais_pendentes()

    for s in pendentes:
        try:
            estrategia = db.txt(s.get("estrategia"))

            # Não mexer nos sinais das estratégias antigas.
            if not estrategia.startswith(PREFIXO_ESTRATEGIA):
                continue

            if inicio and s.get("criado_em") and s["criado_em"] < inicio:
                continue

            base = por_rodada.get(str(s["rodada_base"]))
            if not base:
                continue

            idx = pos.get(base["id"])
            if idx is None:
                continue

            off = max(1, int(s.get("alvo_offset") or 1))
            alvo_idx = idx + off

            if alvo_idx >= len(hist):
                continue

            alvo = hist[alvo_idx]

            real = _normalizar_cor(alvo.get("cor"))
            prevista = _normalizar_cor(s.get("cor_prevista"))

            if real not in ("R", "P", "W"):
                continue

            if prevista not in ("R", "P"):
                continue

            # Regra operacional do script:
            # somente a cor prevista = WIN.
            # W ou a cor oposta = LOSS.
            resultado = (
                RESULTADO_WIN
                if real == prevista
                else RESULTADO_LOSS
            )

            ok = db.resolver_sinal(
                s["id"],
                {
                    "rodada_resultado": str(alvo["rodada_id"]),
                    "cor_resultado": real,
                    "resultado": resultado,
                    "resolvido_em": now,
                },
            )

            if not ok:
                continue

            count += 1

            emoji = "✅" if resultado == RESULTADO_WIN else "❌"

            print(
                f"{emoji} RESULTADO | "
                f"estratégia={estrategia} | "
                f"entrada={_nome(prevista)} | "
                f"real={_nome(real)} | "
                f"rodada={alvo['rodada_id']} | "
                f"{resultado}",
                flush=True,
            )

            enviar_telegram(
                f"{emoji} *RESULTADO — {resultado}*\n\n"
                f"🧠 Estratégia: *{estrategia}*\n"
                f"🎯 Entrada: *{_nome(prevista)}*\n"
                f"🎲 Resultado: *{_nome(real)}*\n"
                f"🔢 Rodada: *{alvo['rodada_id']}*"
            )

        except Exception as e:
            print(
                f"❌ Erro resolvendo sinal {s.get('id')}: {e}",
                flush=True,
            )

    if count:
        print(
            f"📊 Sinais MULTI-GATILHOS resolvidos: {count}",
            flush=True,
        )

    return count


# ---------------------------------------------------------------------
# ESTADO / DASHBOARD
# ---------------------------------------------------------------------

def _estado(now):
    est = db.estatisticas()
    estado = db.ler_estado()
    pend = [
        s for s in db.sinais_pendentes()
        if db.txt(s.get("estrategia")).startswith(PREFIXO_ESTRATEGIA)
    ]

    ultimo = pend[-1] if pend else None

    db.atualizar_estado(
        {
            "wins": est["wins"],
            "losses": est["losses"],
            "whites": est["whites_internos"],
            "profit": round(est["profit"], 2),
            "sinal_ativo": (
                ultimo["estrategia"]
                if ultimo else ""
            ),
            "cor_sinal": (
                ultimo["cor_prevista"]
                if ultimo else ""
            ),
            "ultima_estrategia": (
                ultimo["estrategia"]
                if ultimo
                else db.txt(estado.get("ultima_estrategia"))
            ),
            "atualizado_em": now,
        }
    )

    return est


# ---------------------------------------------------------------------
# ENTRADA PRINCIPAL DO COLLECTOR
# ---------------------------------------------------------------------

def processar_novo_resultado(rodada_id, color, roll):
    """
    Chamado pelo coletor a cada nova rodada completa.

    Ordem:
    1) resolve sinal anterior;
    2) carrega histórico até a rodada atual;
    3) recalcula TODOS os padrões pós-branco;
    4) verifica se a rodada atual é exatamente a 3ª após o último branco;
    5) se o padrão tiver gatilho, cria sinal para a próxima rodada.
    """
    global _iniciado

    if not _iniciado and not init_engine_db():
        return None

    try:
        rid = str(rodada_id)
        rodada = db.buscar_rodada(rid)

        if not rodada:
            return None

        now = db.agora()
        estado = db.ler_estado()

        motor = db.to_bool(
            estado.get("motor_ativo")
        )

        ultimo = db.txt(
            estado.get("ultima_rodada_processada")
        )

        # Evita processar a mesma rodada duas vezes.
        # Ainda tenta resolver um sinal pendente.
        if ultimo == rid:
            if motor:
                _resolver_pendentes(
                    rodada["id"],
                    now,
                )
            return None

        if motor:
            # Primeiro resolve o alvo de sinais anteriores.
            _resolver_pendentes(
                rodada["id"],
                now,
            )

            hist = db.carregar_historico(
                ate_id=rodada["id"]
            )

            # O backtest é mantido apenas como diagnóstico.
            # Não interfere na geração do sinal.
            if len(hist) >= 20:
                bt = backtest_historico(hist)

                print(
                    f"📊 BACKTEST DIAGNÓSTICO | "
                    f"Entradas={bt['entradas']} | "
                    f"Wins={bt['wins']} | "
                    f"Losses={bt['losses']} | "
                    f"Taxa={bt['taxa']:.2f}% | "
                    f"Saldo={bt['saldo']:+d}",
                    flush=True,
                )

            g = detectar_sinal_pos_branco(hist)

            if g:
                _criar_sinal(
                    g,
                    now,
                )
            else:
                # Mensagem somente quando a rodada não gerou gatilho.
                indices_brancos = [
                    i for i, h in enumerate(hist)
                    if h["cor"] == "W"
                ]

                if indices_brancos:
                    ultimo_w = indices_brancos[-1]
                    distancia = (
                        len(hist) - 1 - ultimo_w
                    )

                    if distancia <= TAMANHO_JANELA:
                        print(
                            f"🔎 PÓS-BRANCO | "
                            f"distância={distancia}/3 | "
                            f"nenhum gatilho aplicável.",
                            flush=True,
                        )

        # Só marca como processada depois do motor.
        db.atualizar_estado(
            {
                "ultima_rodada_processada": rid,
                "atualizado_em": now,
            }
        )

        est = _estado(now)
        estado_final = db.ler_estado()

        return {
            "ativo": motor,
            "estrategia": (
                db.txt(
                    estado_final.get(
                        "ultima_estrategia"
                    )
                )
                or None
            ),
            "cor": (
                db.txt(
                    estado_final.get(
                        "cor_sinal"
                    )
                )
                or None
            ),
            "wins": est["wins"],
            "losses": est["losses"],
            "profit": est["profit"],
        }

    except Exception as e:
        print(
            f"❌ Erro no motor: {e}",
            flush=True,
        )
        return None


def reconciliar_todos_sinais():
    """
    Mantém a API antiga do dashboard.
    Não cria sinais retroativos; apenas atualiza o estado.
    """
    if not _iniciado and not init_engine_db():
        return None

    est = _estado(db.agora())

    return {
        "resolvidos": 0,
        **est,
    }


def obter_status_motor():
    if not _iniciado and not init_engine_db():
        return {
            "ativo": False,
            "sinal": None,
            "cor": None,
            "estrategia": None,
            "wins": 0,
            "losses": 0,
            "pendentes": 0,
            "profit": 0.0,
        }

    est = _estado(db.agora())
    estado = db.ler_estado()

    pend = [
        s for s in db.sinais_pendentes()
        if db.txt(s.get("estrategia")).startswith(PREFIXO_ESTRATEGIA)
    ]

    s = pend[-1] if pend else None

    return {
        "ativo": db.to_bool(
            estado.get("motor_ativo")
        ),
        "sinal": bool(s),
        "cor": (
            s["cor_prevista"]
            if s else None
        ),
        "estrategia": (
            s["estrategia"]
            if s else None
        ),
        "wins": est["wins"],
        "losses": est["losses"],
        "pendentes": len(pend),
        "profit": est["profit"],
        "ciclo_ativo": bool(s),
        "tentativa_atual": (
            int(s["tentativa"] or 1)
            if s else 0
        ),
    }
