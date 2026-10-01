# =====================================================================
# BLAZE STRATEGY ENGINE — MACHINE LEARNING (RANDOM FOREST)
# =====================================================================

import os
from datetime import datetime
import requests
import numpy as np
from sklearn.ensemble import RandomForestClassifier
import sheets_db as db

APOSTA_BASE = 1.0
RESULTADO_PENDENTE = "PENDENTE"
RESULTADO_WIN = "WIN"
RESULTADO_LOSS = "LOSS"

PREFIXO_ESTRATEGIA = "MACHINE LEARNING — RANDOM FOREST"

JANELA_HISTORICO = 5
LIMIAR_CONFIANCA = 0.55
MIN_REGISTROS_TREINO = 30

NOMES = {
    "R": "VERMELHO",
    "P": "PRETO",
    "W": "BRANCO",
}

MAPEAMENTO_NUMERICO = {
    "R": 0,
    "P": 1,
    "W": 2,
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

    if "VERMELHO" in v or v in {"R", "RED", "V", "VI", "0"}:
        return "R"
    if "PRETO" in v or v in {"P", "BLACK", "B", "1"}:
        return "P"
    if "BRANCO" in v or v in {"W", "WHITE", "2"}:
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
            "✅ Motor MACHINE LEARNING (Random Forest) inicializado.",
            flush=True,
        )
    else:
        print("❌ Falha inicializando motor.", flush=True)

    return _iniciado


# ---------------------------------------------------------------------
# 1. INTELIGÊNCIA ARTIFICIAL / MACHINE LEARNING
# ---------------------------------------------------------------------

def treinar_e_prever(hist):
    if len(hist) < MIN_REGISTROS_TREINO + JANELA_HISTORICO:
        return None

    valores_numericos = []
    for item in hist:
        cor_norm = _normalizar_cor(item.get("cor"))
        if cor_norm in MAPEAMENTO_NUMERICO:
            valores_numericos.append(MAPEAMENTO_NUMERICO[cor_norm])

    if len(valores_numericos) < MIN_REGISTROS_TREINO + JANELA_HISTORICO:
        return None

    X = []
    y = []

    for i in range(JANELA_HISTORICO, len(valores_numericos) - 1):
        cor_seguinte = valores_numericos[i + 1]
        if cor_seguinte in [0, 1]:
            features = valores_numericos[i - JANELA_HISTORICO + 1 : i + 1]
            X.append(features)
            y.append(cor_seguinte)

    if len(X) < MIN_REGISTROS_TREINO:
        return None

    X = np.array(X)
    y = np.array(y)

    modelo = RandomForestClassifier(
        n_estimators=100,
        max_depth=6,
        min_samples_split=20,
        random_state=42
    )
    modelo.fit(X, y)

    features_atuais = np.array([valores_numericos[-JANELA_HISTORICO:]])

    probabilidades = modelo.predict_proba(features_atuais)[0]
    
    # Trata retornos onde o modelo só aprendeu uma classe na amostra
    if len(probabilidades) < 2:
        return None

    prob_vermelho = probabilidades[0]
    prob_preto = probabilidades[1]

    if prob_vermelho >= LIMIAR_CONFIANCA:
        cor_prevista = "R"
        confianca = prob_vermelho
    elif prob_preto >= LIMIAR_CONFIANCA:
        cor_prevista = "P"
        confianca = prob_preto
    else:
        return None

    return {
        "estrategia": f"{PREFIXO_ESTRATEGIA} — Confiança: {confianca * 100:.1f}%",
        "cor_entrada": cor_prevista,
        "cor_regra": cor_prevista,
        "alvo_offset": 1,
        "confianca": confianca,
        "prob_vermelho": prob_vermelho,
        "prob_preto": prob_preto,
        "rodada_base": hist[-1]["rodada_id"],
    }


# ---------------------------------------------------------------------
# CRIAÇÃO DO SINAL
# ---------------------------------------------------------------------

def _criar_sinal(g, now):
    base = str(g["rodada_base"])
    estrategia = g["estrategia"]

    if any(
        s["rodada_base"] == base
        and s["estrategia"].startswith(PREFIXO_ESTRATEGIA)
        for s in db.sinais_todos()
    ):
        return False

    cid = f"RFML-{base}-{int(datetime.now().timestamp() * 1000)}"

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
        "🤖 NOVO SINAL — MACHINE LEARNING (RANDOM FOREST)\n"
        "============================================================\n"
        f"📊 Confiança Modelo : {g['confianca'] * 100:.1f}%\n"
        f"🔴 Prob. Vermelho   : {g['prob_vermelho'] * 100:.1f}%\n"
        f"⚫ Prob. Preto      : {g['prob_preto'] * 100:.1f}%\n"
        f"👉 Entrada          : {_nome(g['cor_entrada'])}\n"
        f"🎲 Rodada base      : {base}\n"
        f"⏭️ Alvo              : próxima rodada (+1)\n"
        "============================================================",
        flush=True,
    )

    enviar_telegram(
        "🤖 *NOVO SINAL — RANDOM FOREST (ML)* 🤖\n\n"
        f"🎯 Entrada: *{_nome(g['cor_entrada'])}*\n"
        f"📊 Confiança: *{g['confianca'] * 100:.1f}%*\n"
        f"🔴 Vermelho: {g['prob_vermelho'] * 100:.1f}% | ⚫ Preto: {g['prob_preto'] * 100:.1f}%\n"
        f"🎲 Rodada Base: *{base}*\n"
        "⏭️ Alvo: *PRÓXIMA RODADA*\n"
        "💵 Valor: R$ 1,00"
    )

    return True


# ---------------------------------------------------------------------
# RESOLUÇÃO DOS SINAIS
# ---------------------------------------------------------------------

def _resolver_pendentes(atual_id, now):
    hist = db.carregar_historico()

    pos = {h["id"]: i for i, h in enumerate(hist)}
    por_rodada = {str(h["rodada_id"]): h for h in hist}

    estado = db.ler_estado()
    inicio = db.txt(estado.get("inicio_sessao"))

    count = 0
    pendentes = db.sinais_pendentes()

    for s in pendentes:
        try:
            estrategia = db.txt(s.get("estrategia"))

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

            if real not in ("R", "P", "W") or prevista not in ("R", "P"):
                continue

            resultado = RESULTADO_WIN if real == prevista else RESULTADO_LOSS

            ok = db.resolver_sinal(
                s["id"],
                {
                    "rodada_resultado": str(alvo["rodada_id"]),
                    "cor_resultado": real,
                    "resultado": resultado,
                    "resolvido_em": now,
                },
            )

            if ok:
                count += 1
                emoji = "✅" if resultado == RESULTADO_WIN else "❌"
                print(
                    f"{emoji} RESULTADO ML | entrada={_nome(prevista)} | real={_nome(real)} | {resultado}",
                    flush=True,
                )

        except Exception as e:
            print(f"❌ Erro resolvendo sinal {s.get('id')}: {e}", flush=True)

    return count


# ---------------------------------------------------------------------
# ESTADO E PONTO DE ENTRADA DO COLETOR
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
            "profit": round(est["profit"], 2),
            "sinal_ativo": ultimo["estrategia"] if ultimo else "",
            "cor_sinal": ultimo["cor_prevista"] if ultimo else "",
            "atualizado_em": now,
        }
    )

    return est


def processar_novo_resultado(rodada_id, color, roll):
    """
    Chamado pelo coletor a cada nova rodada recebida da API.
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

        motor = db.to_bool(estado.get("motor_ativo"))
        ultimo = db.txt(estado.get("ultima_rodada_processada"))

        if ultimo == rid:
            if motor:
                _resolver_pendentes(rodada["id"], now)
            return None

        if motor:
            _resolver_pendentes(rodada["id"], now)

            hist = db.carregar_historico(ate_id=rodada["id"])

            g = treinar_e_prever(hist)
            if g:
                _criar_sinal(g, now)

        db.atualizar_estado(
            {
                "ultima_rodada_processada": rid,
                "atualizado_em": now,
            }
        )

        est = _estado(now)
        return {"ativo": motor, "wins": est["wins"], "losses": est["losses"], "profit": est["profit"]}

    except Exception as e:
        print(f"❌ Erro no motor ML: {e}", flush=True)
        return None
