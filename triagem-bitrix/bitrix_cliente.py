"""Cliente REST único do Bitrix24 para todos os scripts da triagem.

Junta, num só lugar, o que cada script reimplementava do seu jeito:
  - webhook lido por `seguranca.ler_segredo("webhook_bitrix")` (nada de varrer outros projetos, nada de /tmp);
  - limite de requisições POR HOST entre processos (`rede_bitrix.limitador_padrao()`);
  - disjuntor persistido por finalidade (`rede_bitrix.circuito(...)`);
  - classificação de erro pelo campo `error` (`rede_bitrix.classificar`) e retry só quando a política manda;
  - nada de segredo em log/exceção (`detalhe` já mascarado).

Uso:
    from bitrix_cliente import chamar, listar, FalhaBitrix
    r = chamar("crm.contact.get", {"id": 10})            # dict da resposta (contém "result")
    contatos = listar("crm.contact.list", {"filter": {"NAME": "Ana"}, "select": ["ID", "NAME"]})

Leitura repete só REDE/SERVIDOR dentro da chamada (poucas vezes, com espera).
RATE_LIMIT/OPERATING levantam FalhaBitrix com `pausa_rodada=True`: o chamador para a rodada.
Escrita (`escrita=True`) NUNCA repete após timeout/reset — pode ter executado.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

import rede_bitrix as rb
from circuit_breaker import CircuitOpenError  # noqa: F401  (reexport)
from rede_bitrix import FalhaBitrix  # noqa: F401  (reexport para os consumidores)

TIMEOUT_PADRAO = 25
TENTATIVAS_LEITURA = 3

_webhook_cache: str | None = None


def webhook() -> str:
    """URL base do webhook (termina com '/'). Levanta seguranca.SegredoAusente se não houver."""
    global _webhook_cache
    if _webhook_cache is None:
        import seguranca
        valor = seguranca.ler_segredo("webhook_bitrix") or ""
        _webhook_cache = valor if valor.endswith("/") else valor + "/"
    return _webhook_cache


def _post(url: str, params: dict, timeout: float):
    corpo = json.dumps(params or {}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=corpo, method="POST",
                                 headers={"Content-Type": "application/json", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace")


def chamar(metodo: str, params: dict | None = None, *, escrita: bool = False,
           circuito: str = "bitrix-leitura", timeout: float = TIMEOUT_PADRAO,
           tentativas: int | None = None, _transporte=None) -> dict:
    """Chama `metodo` e devolve o JSON da resposta. Erro -> FalhaBitrix(erro classificado).

    `_transporte(url, params, timeout) -> (status, texto)` existe só para testes.
    """
    transporte = _transporte or _post
    disj = rb.circuito(circuito)
    lim = rb.limitador_padrao()
    maximo = 1 if escrita else (tentativas or TENTATIVAS_LEITURA)
    url = webhook() + metodo + ".json"
    n = 0
    while True:
        n += 1
        if not disj.permitido():
            raise CircuitOpenError(f"circuito {circuito} aberto")
        lim.aguardar()
        status, texto, exc = None, "", None
        try:
            status, texto = transporte(url, params or {}, timeout)
        except urllib.error.HTTPError as e:
            status = e.code
            try:
                texto = e.read().decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                texto = ""
        except Exception as e:  # noqa: BLE001  (rede/DNS/timeout -> classificar decide)
            exc = e
        if exc is None and status is not None and 200 <= status < 300:
            try:
                dados = json.loads(texto)
            except ValueError:
                dados = None
            if isinstance(dados, dict) and "error" not in dados:
                disj.registrar_sucesso()
                return dados
        erro = rb.classificar(status, texto, exc, escrita=escrita)
        if erro.abre_circuito_ja:
            disj.abrir(erro.classe.value, duracao_s=erro.espera_s or None, erro=erro.detalhe)
        elif erro.conta_no_circuito:
            disj.registrar_falha(erro.detalhe, motivo=erro.classe.value)
        espera = None if n >= maximo else rb.atraso(n, erro)
        if espera is None or erro.pausa_rodada:
            raise FalhaBitrix(erro)
        time.sleep(espera)


def listar(metodo: str, params: dict | None = None, *, campo_id: str = "ID",
           limite: int | None = None, circuito: str = "bitrix-leitura", _transporte=None) -> list:
    """Paginação estável recomendada pela doc (settings/performance/huge-data.md):
    filtro `>ID` + `order ID ASC` + `start=-1`. Lê TODAS as páginas (não só as 50 primeiras)."""
    base = dict(params or {})
    filtro = dict(base.pop("filter", {}) or {})
    base["order"] = {campo_id: "ASC"}
    base["start"] = -1
    itens: list = []
    ultimo = None
    while True:
        f = dict(filtro)
        if ultimo is not None:
            f[">" + campo_id] = ultimo
        resp = chamar(metodo, dict(base, filter=f), circuito=circuito, _transporte=_transporte)
        pagina = resp.get("result") or []
        if isinstance(pagina, dict):  # alguns métodos devolvem {"items": [...]}
            pagina = pagina.get("items") or pagina.get("tasks") or []
        if not pagina:
            return itens
        itens.extend(pagina)
        if limite and len(itens) >= limite:
            return itens[:limite]
        novo = pagina[-1].get(campo_id) or pagina[-1].get(campo_id.lower())
        if novo is None or novo == ultimo:
            return itens
        ultimo = novo
        # Sem corte por `len(pagina) < 50`: há métodos com página menor que 50 (achado A1 do
        # Revisor OpenBot). Para só por página vazia ou id que não avança.
