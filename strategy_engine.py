# =====================================================================
# BLAZE STRATEGY ENGINE — ELITE + POS_BRANCO + QUEBRA_ALT
# =====================================================================
import os
from datetime import datetime
import requests
import sheets_db as db

APOSTA_BASE = 1.0
RESULTADO_PENDENTE = "PENDENTE"
RESULTADO_WIN = "WIN"
RESULTADO_LOSS = "LOSS"
NOMES = {"R":"VERMELHO", "P":"PRETO", "W":"BRANCO"}
_iniciado = False


def enviar_telegram(mensagem):
    token=os.getenv("TELEGRAM_TOKEN","").strip(); chat_id=os.getenv("TELEGRAM_CHAT_ID","").strip()
    if not token or not chat_id:
        print("⚠️ Telegram não configurado.",flush=True); return False
    try:
        r=requests.post(f"https://api.telegram.org/bot{token}/sendMessage",data={"chat_id":chat_id,"text":mensagem,"parse_mode":"Markdown","disable_web_page_preview":True},timeout=10)
        r.raise_for_status(); j=r.json()
        if not j.get("ok"): print(f"❌ Telegram recusou: {j}",flush=True); return False
        print("📨 Telegram enviado.",flush=True); return True
    except Exception as e:
        print(f"❌ Erro Telegram: {e}",flush=True); return False


def init_engine_db():
    global _iniciado
    _iniciado=db.init_db()
    print("✅ Motor ELITE + POS_BRANCO + QUEBRA_ALT inicializado." if _iniciado else "❌ Falha inicializando motor.",flush=True)
    return _iniciado


def _nome(c): return NOMES.get(c,c or "?")


def _historico_estrategico(hist):
    return hist

# ---------------------------------------------------------------------
# ELITE — regras do monitor mestre
# ---------------------------------------------------------------------
def detectar_elite(hist):
    cs=[h["cor"] for h in hist]
    rp=[c for c in cs if c in "RP"]
    n=len(rp)
    if n<4: return None
    if n>=4 and all(c=="P" for c in rp[-4:]) and (n==4 or rp[-5]!="P"):
        return {"estrategia":"ELITE — Run de 4 Pretos → Continuação","cor_entrada":"P","cor_regra":"P","alvo_offset":1,"padrao":"PPPP","rodada_base":hist[-1]["rodada_id"]}
    if n>=6 and all(c=="R" for c in rp[-6:]):
        return {"estrategia":"ELITE — Run de 6 Vermelhos → Inversão","cor_entrada":"P","cor_regra":"R","alvo_offset":1,"padrao":"RRRRRR","rodada_base":hist[-1]["rodada_id"]}
    if n>=5 and all(c=="R" for c in rp[-5:]) and (n==5 or rp[-6]!="R"):
        return {"estrategia":"ELITE — Run de 5 Vermelhos → Inversão","cor_entrada":"P","cor_regra":"R","alvo_offset":1,"padrao":"RRRRR","rodada_base":hist[-1]["rodada_id"]}
    return None

# ---------------------------------------------------------------------
# PÓS-BRANCO — alvo físico futuro, conforme regra auditada
# ---------------------------------------------------------------------
def detectar_pos_branco(hist):
    if not hist: return None
    i=len(hist)-1
    if hist[i]["cor"]!="W" or i<1: return None
    cor=hist[i-1]["cor"]
    if cor not in "RP": return None
    run=1; p=i-2
    while p>=0 and hist[p]["cor"]==cor:
        run+=1; p-=1
    passo=0; prevista=None
    if run==2 and cor=="R": passo=3; prevista="P"
    elif run==1 and cor=="P": passo=2; prevista="R"
    elif run==3 and cor=="P": passo=2; prevista="R"
    if not passo: return None
    return {"estrategia":f"PÓS-BRANCO — Run {run} { _nome(cor) } → {_nome(prevista)}","cor_entrada":prevista,"cor_regra":cor,"alvo_offset":passo,"padrao":"".join(x["cor"] for x in hist[max(0,i-run-1):i+1]),"rodada_base":hist[i]["rodada_id"]}

# ---------------------------------------------------------------------
# QUEBRA_ALT — somente depois da rodada atual completa; alvo PRÓXIMA
# ---------------------------------------------------------------------
def detectar_quebra_alt(hist):
    if len(hist)<7: return None
    cs=[h["cor"] for h in hist]; ult=cs[-4:]
    if any(c not in "RP" for c in ult): return None
    if not (ult[0]!=ult[1] and ult[1]!=ult[2] and ult[2]!=ult[3]): return None
    idx=len(cs)-4; inicio=cs[idx]; run=1; p=idx-1
    while p>=0 and cs[p]==inicio: run+=1; p-=1
    if inicio!="R" or run not in (3,4): return None
    ultima=cs[-1]; prevista="P" if ultima=="R" else "R"
    return {"estrategia":f"QUEBRA_ALT — Run {run} R + 3 Alternâncias","cor_entrada":prevista,"cor_regra":"R","alvo_offset":1,"padrao":"".join(ult),"rodada_base":hist[-1]["rodada_id"]}


def detectar_todas(hist):
    out=[]
    for fn in (detectar_elite,detectar_pos_branco,detectar_quebra_alt):
        try:
            g=fn(hist)
            if g: out.append(g)
        except Exception as e: print(f"⚠️ Erro em {fn.__name__}: {e}",flush=True)
    return out


def _criar_sinal(g, now):
    base=str(g["rodada_base"])
    # Dedup por estratégia + rodada base; estratégias diferentes podem disparar juntas.
    if any(s["rodada_base"]==base and s["estrategia"]==g["estrategia"] for s in db.sinais_todos()): return False
    cid=f"{g['estrategia'][:8]}-{base}-{int(datetime.now().timestamp()*1000)}"
    db.inserir_sinal({"rodada_base":base,"estrategia":g["estrategia"],"cor_prevista":g["cor_entrada"],"resultado":RESULTADO_PENDENTE,"tentativa":1,"ciclo_id":cid,"cor_regra":g.get("cor_regra"),"cor_entrada":g["cor_entrada"],"valor_aposta":APOSTA_BASE,"alvo_offset":g.get("alvo_offset",1)})
    enviar_telegram("🚨 *NOVO SINAL* 🚨\n\n"+f"🧠 Estratégia: *{g['estrategia']}*\n"+f"🎯 Entrada: *{_nome(g['cor_entrada'])}*\n"+f"🔄 Padrão: *{g.get('padrao','')}*\n"+f"🎲 Rodada base: *{base}*\n"+f"⏭️ Alvo: *+{g.get('alvo_offset',1)} rodada(s)*\n💵 Valor: R$ 1,00")
    print(f"🔥 SINAL | {g['estrategia']} | {_nome(g['cor_entrada'])} | base={base} | alvo +{g.get('alvo_offset',1)}",flush=True)
    return True


def _resolver_pendentes(atual_id, now):
    hist=db.carregar_historico(); pos={h["id"]:i for i,h in enumerate(hist)}; por={h["rodada_id"]:h for h in hist}
    estado=db.ler_estado(); inicio=db.txt(estado.get("inicio_sessao")); count=0
    for s in db.sinais_pendentes():
        if inicio and s.get("criado_em") and s["criado_em"]<inicio: continue
        base=por.get(str(s["rodada_base"]));
        if not base: continue
        idx=pos.get(base["id"]); off=max(1,int(s.get("alvo_offset") or 1)); alvo_idx=(idx+off) if idx is not None else None
        if alvo_idx is None or alvo_idx>=len(hist): continue
        alvo=hist[alvo_idx]
        if int(alvo["id"])!=int(atual_id): continue
        real=alvo["cor"]; prevista=s["cor_prevista"]
        resultado=RESULTADO_WIN if real==prevista else RESULTADO_LOSS
        if not db.resolver_sinal(s["id"],{"rodada_resultado":str(alvo["rodada_id"]),"cor_resultado":real,"resultado":resultado,"resolvido_em":now}): continue
        count+=1
        emoji="✅" if resultado==RESULTADO_WIN else "❌"
        enviar_telegram(f"{emoji} *RESULTADO — {resultado}*\n\n🧠 Estratégia: *{s['estrategia']}*\n🎯 Entrada: *{_nome(prevista)}*\n🎲 Resultado: *{_nome(real)}*\n🔢 Rodada: *{alvo['rodada_id']}*")
    return count


def _estado(now):
    est=db.estatisticas(); estado=db.ler_estado(); pend=db.sinais_pendentes()
    ultimo=pend[-1] if pend else None
    db.atualizar_estado({"wins":est["wins"],"losses":est["losses"],"whites":est["whites_internos"],"profit":round(est["profit"],2),"sinal_ativo":ultimo["estrategia"] if ultimo else "","cor_sinal":ultimo["cor_prevista"] if ultimo else "","ultima_estrategia":ultimo["estrategia"] if ultimo else db.txt(estado.get("ultima_estrategia")),"atualizado_em":now})
    return est


def processar_novo_resultado(rodada_id,color,roll):
    global _iniciado
    if not _iniciado and not init_engine_db(): return None
    try:
        rid=str(rodada_id); rodada=db.buscar_rodada(rid)
        if not rodada: return None
        now=db.agora(); estado=db.ler_estado(); motor=db.to_bool(estado.get("motor_ativo")); ultimo=db.txt(estado.get("ultima_rodada_processada"))
        if ultimo==rid: return None
        db.atualizar_estado({"ultima_rodada_processada":rid,"atualizado_em":now})
        if motor:
            _resolver_pendentes(rodada["id"],now)
            hist=db.carregar_historico(ate_id=rodada["id"])
            for g in detectar_todas(hist): _criar_sinal(g,now)
        est=_estado(now)
        return {"ativo":motor,"estrategia":db.txt(db.ler_estado().get("ultima_estrategia")) or None,"cor":db.txt(db.ler_estado().get("cor_sinal")) or None,"wins":est["wins"],"losses":est["losses"],"profit":est["profit"]}
    except Exception as e:
        print(f"❌ Erro no motor: {e}",flush=True); return None


def reconciliar_todos_sinais():
    if not _iniciado and not init_engine_db(): return None
    est=_estado(db.agora()); return {"resolvidos":0,**est}


def obter_status_motor():
    if not _iniciado and not init_engine_db(): return {"ativo":False,"sinal":None,"cor":None,"estrategia":None,"wins":0,"losses":0,"pendentes":0,"profit":0.0}
    est=_estado(db.agora()); estado=db.ler_estado(); pend=db.sinais_pendentes(); s=pend[-1] if pend else None
    return {"ativo":db.to_bool(estado.get("motor_ativo")),"sinal":bool(s),"cor":s["cor_prevista"] if s else None,"estrategia":s["estrategia"] if s else None,"wins":est["wins"],"losses":est["losses"],"pendentes":est["pendentes"],"profit":est["profit"],"ciclo_ativo":bool(s),"tentativa_atual":int(s["tentativa"] or 1) if s else 0}
