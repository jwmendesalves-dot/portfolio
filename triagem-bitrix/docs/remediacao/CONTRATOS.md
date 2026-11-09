# Contratos compartilhados (fonte única)

> **Pós-onda 1 (implementado e comitado):** os módulos existem; a verdade é o código + os relatórios `docs/remediacao/frentes/{B,S,R,PT}-*.md`. Diferenças em relação ao texto abaixo que os consumidores PRECISAM saber:
> - `rede_bitrix.proxima_tentativa(...)` devolve **instante epoch** (não segundos); para segundos use `rede_bitrix.atraso(...)`. Para filas entre ciclos use `fila=True` (10 min a 6 h). `classificar(..., download=True)` para host de download; `escrita=True` em escrita (timeout/reset NÃO repetem). Classes extras `SERVIDOR` (5xx) e `INVALIDO`.
> - `LimitadorHost()` sem argumentos lê `BITRIX_RATE`/`BITRIX_RAJADA`/`BITRIX_RATELIMIT_FILE`.
> - `estado_store`: extras `trava_arquivo`, `dono_do_campo`, `limpar_tmp_orfaos`; patch de `atualizar_lotes` aceita `_resolver_motivos`; `atualizar_lotes` devolve só lotes com conflito. Lotes ganham `_donos_campos`, `_motivos_triagem`, `motivos_resolvidos`. `nome` é do dono `identificacao`. Triagem não desfaz arquivo `baixado` com sha256.
> - `estado_lote.resolver_revisao(...)` existe; motivos de outros donos só saem por resolução explícita.
> - `cpf_util.mascarar` existe e devolve `"***"` para valor sem 11 dígitos; `cpf_util.formatar` levanta `ValueError` sem 11 dígitos. `seguranca.SegredoInseguro`/`SegredoInvalido` (filhas de `SegredoAusente`); webhook só `*.bitrix24.<tld>`.
> - **Pós-onda 4 / Z3 (integração):** `seguranca.SegredoRecusado`/`modo_estrito()`, cobertura nova de `mascarar`, `triagem_grupo.ConfigInvalida` (código de saída 3), campo de lote `cpfs_invalidos` (dono `triagem`), `bitrix_cliente.listar` sem corte por página < 50 e `LimitadorHost.aguardar(timeout=)` cobrindo a espera pela trava — detalhes nas seções abaixo.
> - Fallbacks `# TODO`/import tardio de `seguranca`/`rede_bitrix` em pergunta_traco.py, vigia-bot-triagem.py, esperar_bot_liberar.py, circuit_breaker.py, estado_lote.py podem virar import direto (dono de cada arquivo).

Todas as frentes usam estes módulos. Quem é **produtor** (onda 1) implementa exatamente estas assinaturas; quem é **consumidor** (ondas 2+) importa daqui e não reimplementa.
Python 3.9, stdlib. Produtor pode ACRESCENTAR funções; não pode mudar assinatura listada sem registrar no relatório.

---

## `caminhos.py` (orquestrador — pronto)
```python
BASE          # $TRIAGEM_BASE ou /opt/triagem-bitrix
SEPARADOR     # $AGENTE_SEPARADOR_BASE ou /opt/agente-separador
SEGREDOS_DIR  # $TRIAGEM_SEGREDOS_DIR ou /etc/triagem-bitrix
em_base(*p) -> str ; em_separador(*p) -> str
```

## `estado_store.py` (produtor: frente B, onda 1)
```python
class EstadoCorrompido(Exception)          # JSON inválido / raiz não-dict. NUNCA vira {}.
ARQ_ESTADO                                 # $ESTADO_JSON ou caminhos.em_base("estado.json")
ler_estado(caminho=None) -> dict           # ausente -> {} ; corrompido -> EstadoCorrompido (e preserva cópia .corrompido-<ts> uma vez)
gravar_atomico(estado, caminho=None)       # tmp exclusivo (pid+uuid), 0600, fsync arquivo + fsync diretório, os.replace
atualizar_estado(mutador, caminho=None, timeout=120) -> dict
    # flock -> relê -> mutador(estado) (pode devolver novo dict) -> grava. Exceção no mutador = nada gravado.
    # Regra p/ consumidores: HTTP, hashing e leitura demorada FORA do mutador; dentro só aplicar diferenças já calculadas.
atualizar_lotes(mudancas: dict[str, dict], dono: str, caminho=None) -> dict[str, list[str]]
    # aplica patch POR CAMPO em lotes existentes respeitando PROPRIEDADE_CAMPOS; devolve conflitos (campo negado) por lote_id.
mesclar_lote(atual: dict, novo: dict, dono: str) -> tuple[dict, list[str]]
    # merge por propriedade de campo: o `dono` só escreve campos que são dele; campos de outros donos em `atual` são preservados.
PROPRIEDADE_CAMPOS: dict[str, str]         # campo -> dono ("triagem" | "identificacao" | "download" | "ponte" | "revisao")
fazer_backup(caminho=None, motivo="") -> str | None       # cópia 0600 em <dir>/backups-estado/estado.json.bak-<ts>
podar_backups(caminho=None, manter_ultimos=24, diarios=14, simular=True) -> list[str]   # devolve o que apagaria/apagou
```

## `estado_lote.py` (produtor: frente B, onda 1) — modelo de estados, funções puras
```python
# valores EXTERNOS já gravados (preservar): status in {"ABERTO","FECHADO_PRONTO","FECHADO_POR_TIMEOUT","FECHADO_POR_JANELA",
#   "FECHADO_POR_INATIVIDADE","CONFLITO"}; identificacao in {"OK","SEM_CPF","SEM_ARQUIVOS","CONFLITO"};
#   fila-enviados: "descartado_duplicata","revisao_duplicata","pendente", ...
ESTADOS_LOGICOS = ("ABERTO","FECHADO","AGUARDANDO_DOWNLOAD","AGUARDANDO_IDENTIFICACAO","REVISAO","PRONTO",
                   "ENFILEIRANDO","ENFILEIRADO","DESCARTADO_DUPLICATA","DIVIDIDO","IGNORADO","ERRO_PERMANENTE")
estado_logico(lote: dict, controle: dict | None = None) -> str
motivos_impeditivos(lote: dict) -> list[str]   # por que NÃO pode ir à fila (vazio = elegível)
pode_enfileirar(lote: dict) -> tuple[bool, list[str]]
    # exige: identificacao OK com CPF válido (cpf_util), não requer_revisao, fechado, todos os arquivos com baixado=True,
    # sha256 e tamanho>0, nenhum arquivo .part/.parcial, não IGNORADO/DIVIDIDO/ERRO_PERMANENTE.
transicao_valida(de: str, para: str) -> bool
marcar_revisao(lote: dict, motivo: str) -> dict   # acrescenta motivo sem apagar os existentes; requer_revisao=True; pronto=False
```

## `cpf_util.py` (produtor: frente S, onda 1)
```python
normalizar(valor) -> str | None     # só dígitos; 11 dígitos -> str; senão None
valido(valor) -> bool               # DV correto, rejeita sequências repetidas (000..., 111...)
formatar(valor) -> str              # 000.000.000-00 (só apresentação)
gerar_para_teste(semente: int) -> str   # CPF sintético com DV válido, para fixtures
```

## `seguranca.py` (produtor: frente S, onda 1)
```python
mascarar(texto: str) -> str          # webhook rest/<id>/<token>, X-Api-Key, token Telegram, CPF (com/sem pontos), "senha: xxx"
    # (Z2) + pares livres token= / access_token= / auth= / refresh_token= / client_secret= em texto livre
    #      e chave JSON/dict ("token": "x", {'auth': 'x'}); nome EXATO (não pega oauth=, mytoken=, tokens=, auth_type=).
    #      Idempotente.
mascarar_url(url: str) -> str
class FiltroMascara(logging.Filter)  # aplica mascarar em record.msg/args
instalar_filtro_log(logger: logging.Logger | None = None) -> None
remover_senhas(texto: str) -> str    # para PERSISTÊNCIA: troca o valor de senha gov.br/INSS por "[senha removida]"
ler_segredo(nome: str, obrigatorio: bool = True) -> str | None
    # ordem: env TRIAGEM_<NOME_MAIUSCULO>  ->  arquivo caminhos.SEGREDOS_DIR/<nome> (recusa se legível por grupo/outros)
    #        -> caminhos legados EXPLÍCITOS e configurados (nunca /tmp, nunca varrer outros projetos)
    # nomes: "webhook_bitrix", "vibecode_key", "telegram_token", "bot_token_100003"
class SegredoAusente(RuntimeError)       # filhas: SegredoInseguro, SegredoInvalido
class SegredoRecusado(RuntimeError)      # (Z2) SÓ com TRIAGEM_SEGREDOS_ESTRITO=1: arquivo de segredo inseguro
    # (0o077/dono estranho, legado aberto, /tmp|/var/tmp, diretório, não-regular). NÃO herda de SegredoAusente
    # de propósito: `except SegredoAusente: open(legado)` não engole a recusa. Mensagem traz "[modo estrito]", nunca o valor.
    # Segredo ausente continua SegredoAusente; sem ESTRITO, arquivo inseguro só avisa (comportamento antigo).
modo_estrito() -> bool                   # os.environ["TRIAGEM_SEGREDOS_ESTRITO"] == "1"
arquivo_temp_privado(sufixo="", prefixo="triagem-") -> str   # em caminhos.em_base("tmp") com 0700/0600, nome único
remover_seguro(caminho: str) -> None
```

## `rede_bitrix.py` (produtor: frente R, onda 1) + `circuit_breaker.py`
```python
class Classe(enum)  # RATE_LIMIT (503 QUERY_LIMIT_EXCEEDED), OPERATING (429 OPERATION_TIME_LIMIT), REDE (timeout/DNS/reset),
                    # URL_EXPIRADA, ACESSO (401/403/ACCESS_DENIED/insufficient_scope/WRONG_AUTH_TYPE), LOOP (ERROR_LOOP_DETECTED),
                    # NAO_ENCONTRADO (404/NOT_FOUND), TAMANHO, TIPO, INTEGRIDADE, OVERLOAD (OVERLOAD_LIMIT), DESCONHECIDO
@dataclass Erro(classe, repetir: bool, espera_s: float, permanente: bool, detalhe: str)  # detalhe já mascarado
classificar(status: int | None, corpo: str = "", exc: BaseException | None = None) -> Erro
proxima_tentativa(tentativas: int, erro: Erro, agora: float | None = None) -> float | None
    # None = desistir (permanente ou excedeu MAX_TENTATIVAS); backoff exponencial com jitter; respeita espera_s
class LimitadorHost   # token bucket ENTRE PROCESSOS (arquivo + flock), padrão 1.5 req/s, rajada 10
    def __init__(self, caminho=None, taxa=1.5, rajada=10)   # caminho padrão $BITRIX_RATELIMIT_FILE ou caminhos.em_base(".bitrix-ratelimit")
    def aguardar(self, custo=1, timeout=60) -> float        # devolve segundos esperados
        # levanta rede_bitrix.TempoEsgotado (TimeoutError) sem consumir token se a espera passaria de `timeout`;
        # o MESMO timeout cobre a espera pela trava (flock) do arquivo do limitador — trava ocupada > timeout = TempoEsgotado.
limitador_padrao() -> LimitadorHost
circuito(nome: str) -> circuit_breaker.CircuitBreaker
    # nomes: "bitrix-leitura", "bitrix-download", "vibecode-leitura", "vibecode-download" — estado persistido em
    # caminhos.em_base(f"circuit-{nome}.json") com aberto_em, motivo, falhas; meia-abertura com UMA sonda; fecha no sucesso.
```

## `triagem_grupo.py` — config (dono: agrupamento; C3/Z3)
```python
class ConfigInvalida(RuntimeError)   # config.json ilegível/não-objeto, fonte_leitura/modo_traco desconhecido,
                                     # historico_paginas inválido. Padrão só vale para chave AUSENTE.
carregar_config() -> dict            # levanta ConfigInvalida; nunca cai silenciosamente no padrão
```
Consumidores (`triagem_grupo.main`, `pergunta_traco`, `checar_midias`, `telegram_grupo`): `ConfigInvalida` vira mensagem
mascarada (`seguranca.mascarar`), sem traceback, **código de saída 3** (`SAIDA_CONFIG = 3`). Em modo `--loop` sob systemd
o processo NÃO sai (evita reinício em laço): relê o config com pausa crescente 60 → 120 → … → 900 s até ficar válido.

## Campos de lote novos (`estado_store.PROPRIEDADE_CAMPOS`)
- `cpfs_invalidos` — dono **`triagem`** (C2b/C4): CPFs vistos no kit cujo DV não confere. Não viram `cpf`, âncora,
  barreira nem chave de CRM; o lote fica com motivo `CPF_INVALIDO`, `requer_revisao=True`, `pronto=False`.

## `bitrix_cliente.py` (orquestrador — pronto, onda 1.5)
Cliente REST ÚNICO do Bitrix. Todo script que chama o portal usa este — não crie outro `urlopen` para a REST.
```python
webhook() -> str                                   # via seguranca.ler_segredo("webhook_bitrix"), termina em "/"
chamar(metodo, params=None, *, escrita=False, circuito="bitrix-leitura", timeout=25, tentativas=None) -> dict
    # limitador por host + disjuntor + classificar; leitura repete só REDE/SERVIDOR conforme política;
    # RATE_LIMIT com pausa_rodada levanta (pare a rodada); escrita nunca repete. Erro -> FalhaBitrix(erro); disjuntor aberto -> CircuitOpenError
listar(metodo, params=None, *, campo_id="ID", limite=None, circuito="bitrix-leitura") -> list
    # paginação estável >ID + order ASC + start=-1 (lê TODAS as páginas)
    # SEM corte por `len(pagina) < 50` (há métodos com página menor): para só com página vazia,
    # id que não avança ou `limite` atingido.
```
Testes: injete `_transporte=lambda url, params, timeout: (status, texto)`; nunca rede.

## `tipos_documento.py` (orquestrador — pronto)
```python
classificar(nome, mime=None) -> "documento" | "midia" | "desconhecido"   # extensão manda; MIME desempata
e_documento(nome, mime=None) -> bool
```
Regra (C1/S10): só `documento` vai ao worker/staging e conta como pendência de download. `midia` fica registrada no lote (campo `midias_ignoradas` ou `tipo` no item de `arquivos`) e nunca é baixada/enviada como documento. `desconhecido` (os ~450 sem extensão) é baixado e reclassificado pelo MIME/assinatura do conteúdo antes de ir à ponte.
