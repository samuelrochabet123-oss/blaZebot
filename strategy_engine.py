# BLAZE STRATEGY ENGINE — ESTRATÉGIA 1
# Gatilho por ciclo pós-branco + média dos rolls
#
# Substitui a lógica MULTI-GATILHOS PÓS-BRANCO da versão anterior,
# mantendo a interface operacional esperada pelo BOT:
#   - sheets_db
#   - sinais
#   - estado do motor
#   - dashboard
#   - Telegram
#
# REGRA DA ESTRATÉGIA 1:
# 1) Localiza o BRANCO.
# 2) Acumula os rolls após esse branco.
# 3) Quando aparece o próximo BRANCO, fecha o ciclo anterior.
# 4) Calcula a média dos rolls do ciclo fechado.
# 5) Média <= 7.5  -> GRANDE (8-14)
# 6) Média >  7.5  -> PEQUENO (1-7)
# 7) O sinal fica pendente.
# 8) O primeiro resultado NÃO-BRANCO após o sinal resolve a entrada.
#
# IMPORTANTE:
# A estratégia original trabalha com GRANDE/PEQUENO, mas o banco do BOT
# trabalha com cores R/P. Para preservar a estrutura do motor:
#   GRANDE  -> P (PRETO / 8-14)
#   PEQUENO -> R (VERMELHO / 1-7)
#
# Esta conversão segue exatamente o texto da Estratégia 1:
# GRANDE (Preto / 8-14) e PEQUENO (Vermelho / 1-7).
# =====================================================================

import os
from datetime import datetime
import requests
import sheets_db as db


# ---------------------------------------------------------------------
# CONFIGURAÇÃO
# ---------------------------------------------------------------------

APOSTA_BASE = 1.0

RESULTADO_PENDENTE = "PENDENTE"
RESULTADO_WIN = "WIN"
RESULTADO_LOSS = "LOSS"

PREFIXO_ESTRATEGIA = "PÓS-BRANCO — MÉDIA DOS ROLLS"

LIMITE_MEDIA = 7.5

NOMES = {
    "R": "VERMELHO",
    "P": "PRETO",
    "W": "BRANCO",
}

_iniciado = False


# ---------------------------------------------------------------------
# UTILITÁRIOS
# ---------------------------------------------------------------------

def _nome(cor):
    return NOMES.get(cor, cor or "?")


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

    if "BRANCO" in v or v in {"W", "WHITE", "0"}:
        return "W"

    return None


def _roll_para_cor(roll):
    """
    Conversão operacional do roll para a classificação da Estratégia 1.

    A Estratégia 1 considera:
      8-14 = GRANDE / PRETO
      1-7  = PEQUENO / VERMELHO
      0    = BRANCO
    """
    try:
        r = int(roll)
    except Exception:
        return None

    if r == 0:
        return "W"

    if 1 <= r <= 7:
        return "R"

    if 8 <= r <= 14:
        return "P"

    return None


def _extrair_roll(item):
    """
    Extrai o roll do registro do histórico.

    O motor prioriza 'roll', que é o campo usado pela Estratégia 1
    original. Se não existir, tenta converter a cor.
    """
    try:
        valor = item.get("roll")
    except Exception:
        valor = None

    if valor is not None and str(valor).strip() != "":
        try:
            return int(float(valor))
        except Exception:
            pass

    cor = _normalizar_cor(item.get("cor"))
    if cor == "W":
        return 0

    # Sem roll não é possível reconstruir a média original.
    return None


def _classificar_resultado(roll):
    if roll is None:
        return None

    try:
        r = int(roll)
    except Exception:
        return None

    if r == 0:
        return "W"

    if 1 <= r <= 7:
        return "R"

    if 8 <= r <= 14:
        return "P"

    return None


def _cor_do_historico(item):
    """
    Obtém a cor de forma robusta.

    Quando o roll existe, ele é usado para manter a mesma classificação
    da Estratégia 1.
    """
    roll = _extrair_roll(item)

    cor_roll = _classificar_resultado(roll)
    if cor_roll is not None:
        return cor_roll

    return _normalizar_cor(item.get("cor"))


# ---------------------------------------------------------------------
# TELEGRAM
# ---------------------------------------------------------------------

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


# ---------------------------------------------------------------------
# INICIALIZAÇÃO
# ---------------------------------------------------------------------

def init_engine_db():
    global _iniciado

    _iniciado = db.init_db()

    if _iniciado:
        print(
            "✅ Motor ESTRATÉGIA 1 — MÉDIA DOS ROLLS inicializado.",
            flush=True,
        )
    else:
        print("❌ Falha inicializando motor.", flush=True)

    return _iniciado


# ---------------------------------------------------------------------
# 1. CONSTRUÇÃO DOS CICLOS PÓS-BRANCO
# ---------------------------------------------------------------------

def _obter_ciclos(hist):
    """
    Reproduz a estrutura da Estratégia 1 original.

    Cada ciclo é formado pelos rolls NÃO-ZERO entre dois brancos.

    Exemplo:

        W | 5 | 9 | 7 | W

    ciclo = [5, 9, 7]
    média = 7.00
    sinal = GRANDE

    Retorna ciclos fechados e o ciclo atual ainda aberto.
    """

    ciclos_fechados = []
    bloco_rolls = []

    branco_anterior = None

    for idx, item in enumerate(hist):
        roll = _extrair_roll(item)

        if roll is None:
            continue

        if roll != 0:
            bloco_rolls.append(
                {
                    "roll": roll,
                    "idx": idx,
                    "rodada_id": item.get("rodada_id"),
                    "id": item.get("id"),
                }
            )
            continue

        # Encontrou BRANCO.
        if len(bloco_rolls) > 0:
            media = (
                sum(x["roll"] for x in bloco_rolls)
                / len(bloco_rolls)
            )

            if media <= LIMITE_MEDIA:
                aposta_tipo = "P"
                aposta_texto = "GRANDE (Preto / 8-14)"
            else:
                aposta_tipo = "R"
                aposta_texto = "PEQUENO (Vermelho / 1-7)"

            ciclos_fechados.append(
                {
                    "branco_idx": idx,
                    "branco_rodada_id": item.get("rodada_id"),
                    "rolls": [x["roll"] for x in bloco_rolls],
                    "tamanho": len(bloco_rolls),
                    "media": media,
                    "aposta_tipo": aposta_tipo,
                    "aposta_texto": aposta_texto,
                    "primeiro_idx": bloco_rolls[0]["idx"],
                    "ultimo_idx": bloco_rolls[-1]["idx"],
                }
            )

        bloco_rolls = []
        branco_anterior = idx

    ciclo_aberto = {
        "rolls": [x["roll"] for x in bloco_rolls],
        "tamanho": len(bloco_rolls),
        "primeiro_idx": (
            bloco_rolls[0]["idx"] if bloco_rolls else None
        ),
        "ultimo_idx": (
            bloco_rolls[-1]["idx"] if bloco_rolls else None
        ),
    }

    return ciclos_fechados, ciclo_aberto


def construir_sinal_media(hist):
    """
    Detecta se a rodada atual acabou de ser um BRANCO que fecha um ciclo.

    Quando isso acontece, cria a decisão exatamente como a Estratégia 1:

        média <= 7.5 -> GRANDE / PRETO
        média >  7.5 -> PEQUENO / VERMELHO

    O sinal aponta para o primeiro resultado NÃO-BRANCO seguinte.
    """

    if not hist:
        return None

    ciclos_fechados, _ = _obter_ciclos(hist)

    if not ciclos_fechados:
        return None

    ciclo = ciclos_fechados[-1]

    ultimo = hist[-1]
    ultimo_roll = _extrair_roll(ultimo)

    # O sinal só nasce quando a rodada atual é o BRANCO
    # que acabou de fechar o ciclo.
    if ultimo_roll != 0:
        return None

    # Não criar novamente o mesmo ciclo.
    rodada_base = str(ultimo.get("rodada_id"))

    return {
        "estrategia": (
            f"{PREFIXO_ESTRATEGIA} — "
            f"{ciclo['aposta_texto']}"
        ),
        "cor_entrada": ciclo["aposta_tipo"],
        "cor_regra": ciclo["aposta_tipo"],
        "alvo_offset": 1,
        "padrao": " → ".join(str(x) for x in ciclo["rolls"]),
        "rodada_base": rodada_base,
        "media": ciclo["media"],
        "tamanho_ciclo": ciclo["tamanho"],
        "rolls_ciclo": ciclo["rolls"],
        "aposta_texto": ciclo["aposta_texto"],
    }


# ---------------------------------------------------------------------
# ANÁLISE HISTÓRICA
# ---------------------------------------------------------------------

def analisar_historico(hist):
    """
    Calcula as mesmas estatísticas básicas exibidas pela Estratégia 1:
      - wins
      - losses
      - taxa
      - saldo
      - maior sequência de wins
      - maior sequência de losses
      - frequência das sequências de losses

    A análise considera somente sinais já resolvidos.
    """

    sinais = [
        s for s in db.sinais_todos()
        if db.txt(s.get("estrategia")).startswith(PREFIXO_ESTRATEGIA)
    ]

    wins = 0
    losses = 0

    seq_atual_wins = 0
    seq_atual_losses = 0
    max_seq_wins = 0
    max_seq_losses = 0

    lista_seq_losses = []

    for s in sinais:
        resultado = db.txt(s.get("resultado"))

        if resultado == RESULTADO_WIN:
            wins += 1

            if seq_atual_losses > 0:
                lista_seq_losses.append(seq_atual_losses)

            seq_atual_wins += 1
            seq_atual_losses = 0

            max_seq_wins = max(max_seq_wins, seq_atual_wins)

        elif resultado == RESULTADO_LOSS:
            losses += 1

            seq_atual_losses += 1
            seq_atual_wins = 0

            max_seq_losses = max(max_seq_losses, seq_atual_losses)

    if seq_atual_losses > 0:
        lista_seq_losses.append(seq_atual_losses)

    taxa = (
        wins / (wins + losses) * 100
        if (wins + losses) > 0
        else 0.0
    )

    from collections import Counter
    contagem_losses = Counter(lista_seq_losses)

    return {
        "wins": wins,
        "losses": losses,
        "taxa": taxa,
        "saldo": wins - losses,
        "max_seq_wins": max_seq_wins,
        "max_seq_losses": max_seq_losses,
        "contagem_losses": contagem_losses,
    }


# ---------------------------------------------------------------------
# BACKTEST DA ESTRATÉGIA 1
# ---------------------------------------------------------------------

def backtest_historico(hist):
    """
    Backtest descritivo da Estratégia 1.

    Para cada ciclo fechado:
      1) calcula a média dos rolls;
      2) decide GRANDE/PRETO ou PEQUENO/VERMELHO;
      3) procura o primeiro resultado NÃO-BRANCO após o branco;
      4) contabiliza WIN/LOSS.

    Este backtest é apenas diagnóstico. Não participa da criação
    do sinal operacional.
    """

    if not hist:
        return {
            "entradas": 0,
            "wins": 0,
            "losses": 0,
            "taxa": 0.0,
            "saldo": 0,
        }

    ciclos_fechados, _ = _obter_ciclos(hist)

    wins = 0
    losses = 0

    for ciclo in ciclos_fechados:
        branco_idx = ciclo["branco_idx"]
        prevista = ciclo["aposta_tipo"]

        alvo = None

        for idx in range(branco_idx + 1, len(hist)):
            cor = _cor_do_historico(hist[idx])

            if cor in ("R", "P"):
                alvo = cor
                break

        if alvo is None:
            continue

        if alvo == prevista:
            wins += 1
        else:
            losses += 1

    entradas = wins + losses

    taxa = (
        wins / entradas * 100
        if entradas
        else 0.0
    )

    return {
        "entradas": entradas,
        "wins": wins,
        "losses": losses,
        "taxa": taxa,
        "saldo": wins - losses,
    }


# ---------------------------------------------------------------------
# CRIAÇÃO DO SINAL
# ---------------------------------------------------------------------

def _criar_sinal(g, now):
    base = str(g["rodada_base"])
    estrategia = g["estrategia"]

    # Não cria duas vezes o mesmo gatilho na mesma rodada-base.
    if any(
        str(s.get("rodada_base")) == base
        and db.txt(s.get("estrategia")) == estrategia
        for s in db.sinais_todos()
    ):
        return False

    cid = (
        f"MEDIA-{base}-"
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

            # Mantém a interface do banco.
            # A resolução real da Estratégia 1 procura o primeiro
            # resultado não-branco após o branco.
            "alvo_offset": 1,
        }
    )

    rolls_texto = ", ".join(
        str(x) for x in g["rolls_ciclo"]
    )

    print(
        "\n"
        "============================================================\n"
        "🎯 NOVO SINAL — ESTRATÉGIA 1 / MÉDIA PÓS-BRANCO\n"
        "============================================================\n"
        f"⚪ Branco base       : {base}\n"
        f"📦 Ciclo             : {rolls_texto}\n"
        f"📏 Tamanho do ciclo  : {g['tamanho_ciclo']}\n"
        f"📊 Média             : {g['media']:.2f}\n"
        f"🎯 Regra             : média {'≤' if g['media'] <= LIMITE_MEDIA else '>'} {LIMITE_MEDIA}\n"
        f"👉 Entrada           : {g['aposta_texto']}\n"
        "⏭️ Alvo              : primeiro resultado NÃO-BRANCO\n"
        "============================================================",
        flush=True,
    )

    enviar_telegram(
        "🚨 *NOVO SINAL — ESTRATÉGIA 1* 🚨\n\n"
        f"⚪ Branco base: *{base}*\n"
        f"📦 Ciclo: *{rolls_texto}*\n"
        f"📏 Tamanho: *{g['tamanho_ciclo']}*\n"
        f"📊 Média: *{g['media']:.2f}*\n"
        f"🎯 Regra: *{'GRANDE' if g['cor_entrada'] == 'P' else 'PEQUENO'}*\n"
        f"🎯 Entrada: *{_nome(g['cor_entrada'])}*\n"
        "⏭️ Alvo: *PRIMEIRO RESULTADO NÃO-BRANCO*\n"
        "💵 Valor: R$ 1,00"
    )

    return True


# ---------------------------------------------------------------------
# RESOLUÇÃO DOS SINAIS
# ---------------------------------------------------------------------

def _resolver_pendentes(atual_id, now):
    """
    Resolve sinais pendentes da Estratégia 1.

    Diferentemente da Estratégia 2, a Estratégia 1 original não trata
    um branco imediatamente posterior como o resultado da entrada.

    O sinal permanece pendente até aparecer o primeiro resultado
    NÃO-BRANCO após o branco que gerou o sinal.

    Isso reproduz a lógica original:

        if pendente_sinal is not None and r != 0:
            resolve WIN/LOSS
    """

    hist = db.carregar_historico()

    if not hist:
        return 0

    pos = {
        h.get("id"): i
        for i, h in enumerate(hist)
    }

    pendentes = db.sinais_pendentes()

    estado = db.ler_estado()
    inicio = db.txt(estado.get("inicio_sessao"))

    count = 0

    for s in pendentes:
        try:
            estrategia = db.txt(s.get("estrategia"))

            # Não mexer nos sinais de outras estratégias.
            if not estrategia.startswith(PREFIXO_ESTRATEGIA):
                continue

            if (
                inicio
                and s.get("criado_em")
                and s["criado_em"] < inicio
            ):
                continue

            base_id = None

            # Localiza a rodada-base pelo rodada_id.
            base_rodada_id = str(s.get("rodada_base"))

            base_idx = None

            for i, h in enumerate(hist):
                if str(h.get("rodada_id")) == base_rodada_id:
                    base_idx = i
                    break

            if base_idx is None:
                continue

            # Procura o primeiro resultado não-branco depois
            # da rodada-base.
            alvo_idx = None

            for i in range(base_idx + 1, len(hist)):
                cor = _cor_do_historico(hist[i])

                if cor in ("R", "P"):
                    alvo_idx = i
                    break

            if alvo_idx is None:
                continue

            alvo = hist[alvo_idx]

            real = _cor_do_historico(alvo)
            prevista = _normalizar_cor(
                s.get("cor_prevista")
            )

            if real not in ("R", "P"):
                continue

            if prevista not in ("R", "P"):
                continue

            resultado = (
                RESULTADO_WIN
                if real == prevista
                else RESULTADO_LOSS
            )

            ok = db.resolver_sinal(
                s["id"],
                {
                    "rodada_resultado": str(
                        alvo.get("rodada_id")
                    ),
                    "cor_resultado": real,
                    "resultado": resultado,
                    "resolvido_em": now,
                },
            )

            if not ok:
                continue

            count += 1

            emoji = (
                "✅"
                if resultado == RESULTADO_WIN
                else "❌"
            )

            print(
                f"{emoji} RESULTADO | "
                f"estratégia={estrategia} | "
                f"entrada={_nome(prevista)} | "
                f"real={_nome(real)} | "
                f"rodada={alvo.get('rodada_id')} | "
                f"{resultado}",
                flush=True,
            )

            enviar_telegram(
                f"{emoji} *RESULTADO — {resultado}*\n\n"
                f"🧠 Estratégia: *{estrategia}*\n"
                f"🎯 Entrada: *{_nome(prevista)}*\n"
                f"🎲 Resultado: *{_nome(real)}*\n"
                f"🔢 Rodada: *{alvo.get('rodada_id')}*"
            )

        except Exception as e:
            print(
                f"❌ Erro resolvendo sinal {s.get('id')}: {e}",
                flush=True,
            )

    if count:
        print(
            f"📊 Sinais da ESTRATÉGIA 1 resolvidos: {count}",
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
        s
        for s in db.sinais_pendentes()
        if db.txt(
            s.get("estrategia")
        ).startswith(PREFIXO_ESTRATEGIA)
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
                if ultimo
                else ""
            ),
            "cor_sinal": (
                ultimo["cor_prevista"]
                if ultimo
                else ""
            ),
            "ultima_estrategia": (
                ultimo["estrategia"]
                if ultimo
                else db.txt(
                    estado.get("ultima_estrategia")
                )
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
      1) Resolve sinal pendente, se apareceu resultado não-branco.
      2) Carrega histórico até a rodada atual.
      3) Verifica se a rodada atual é BRANCO.
      4) Se o branco fechou um ciclo, calcula a média.
      5) Cria o sinal GRANDE/PRETO ou PEQUENO/VERMELHO.
      6) Atualiza o estado do BOT.
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
        # Ainda tenta resolver sinal pendente.
        if ultimo == rid:
            if motor:
                _resolver_pendentes(
                    rodada["id"],
                    now,
                )

            return None

        if motor:
            # Primeiro resolve sinais anteriores.
            _resolver_pendentes(
                rodada["id"],
                now,
            )

            hist = db.carregar_historico(
                ate_id=rodada["id"]
            )

            # Backtest somente diagnóstico.
            if len(hist) >= 20:
                bt = backtest_historico(hist)

                print(
                    f"📊 BACKTEST ESTRATÉGIA 1 | "
                    f"Entradas={bt['entradas']} | "
                    f"Wins={bt['wins']} | "
                    f"Losses={bt['losses']} | "
                    f"Taxa={bt['taxa']:.2f}% | "
                    f"Saldo={bt['saldo']:+d}",
                    flush=True,
                )

            g = construir_sinal_media(hist)

            if g:
                _criar_sinal(
                    g,
                    now,
                )
            else:
                # Diagnóstico do ciclo atual.
                ultimo_roll = _extrair_roll(hist[-1]) if hist else None

                if ultimo_roll == 0:
                    _, ciclo = _obter_ciclos(hist)

                    if ciclo["tamanho"] > 0:
                        print(
                            "🔎 ESTRATÉGIA 1 | "
                            "BRANCO detectado, mas sem ciclo "
                            "fechado com rolls anteriores.",
                            flush=True,
                        )
                    else:
                        print(
                            "🔎 ESTRATÉGIA 1 | "
                            "BRANCO detectado, ciclo vazio.",
                            flush=True,
                        )

                else:
                    _, ciclo = _obter_ciclos(hist)

                    if ciclo["tamanho"] > 0:
                        media_atual = (
                            sum(ciclo["rolls"])
                            / ciclo["tamanho"]
                        )

                        print(
                            "🔎 ESTRATÉGIA 1 | "
                            f"ciclo atual={ciclo['tamanho']} "
                            f"roll(s) | "
                            f"média parcial={media_atual:.2f} | "
                            "aguardando BRANCO para fechar.",
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


# ---------------------------------------------------------------------
# COMPATIBILIDADE COM O DASHBOARD
# ---------------------------------------------------------------------

def reconciliar_todos_sinais():
    """
    Mantém a API antiga do dashboard.

    Não cria sinais retroativos.
    Apenas atualiza o estado.
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
        s
        for s in db.sinais_pendentes()
        if db.txt(
            s.get("estrategia")
        ).startswith(PREFIXO_ESTRATEGIA)
    ]

    s = pend[-1] if pend else None

    return {
        "ativo": db.to_bool(
            estado.get("motor_ativo")
        ),
        "sinal": bool(s),
        "cor": (
            s["cor_prevista"]
            if s
            else None
        ),
        "estrategia": (
            s["estrategia"]
            if s
            else None
        ),
        "wins": est["wins"],
        "losses": est["losses"],
        "pendentes": len(pend),
        "profit": est["profit"],
        "ciclo_ativo": bool(s),
        "tentativa_atual": (
            int(s["tentativa"] or 1)
            if s
            else 0
        ),
    }
