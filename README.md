# Portfólio

## Triagem de documentos do Bitrix24 — auditoria e remediação

[`triagem-bitrix/`](triagem-bitrix/) — sistema em Python que lê grupos de atendimento do
Bitrix24 (conectores WhatsApp), separa a conversa em **lotes por cliente**, baixa os anexos,
identifica nome e CPF, deduplica e entrega cada lote a uma fila de processamento (OCR,
classificação por IA, CRM, planilha e armazenamento).

O trabalho publicado aqui é a **auditoria completa do sistema em produção e a remediação**:

- **101 achados** auditados em 6 frentes (segurança, loteamento, download/estado, fila,
  identificação, testes/operação) — [`docs/auditoria/`](triagem-bitrix/docs/auditoria/).
- Correção por ondas, com equipes de agentes em paralelo e revisão independente; cada
  correção acompanha um teste que falha no código anterior — [`docs/remediacao/`](triagem-bitrix/docs/remediacao/).
- Leitura incremental com cursor (fim dos lotes artificiais), estado gravado de forma
  atômica com dono por campo, cliente HTTP com limitador por host e disjuntor, download
  verificado por sha256, fila idempotente com trava única, máscara de segredos e CPF em todo
  log, modo estrito para arquivos de segredo.
- Decisor **TypeSafe/JEV** opcional (modos desligado / sombra / decide) com roteamento por
  confiança e a regra de que a IA nunca promove documento de dono desconhecido.
- Plano de implantação em systemd com checkpoints, rollback e runbook operacional.
- **1.332 testes** automatizados (offline), incluindo um cenário de ponta a ponta com vários
  ciclos, falha de gravação e reinício.

Documentos de entrada: [`ENTREGA.md`](triagem-bitrix/docs/remediacao/ENTREGA.md) ·
[`MATRIZ-ACHADOS.md`](triagem-bitrix/docs/remediacao/MATRIZ-ACHADOS.md) ·
[`ARQUITETURA-CORRIGIDA.md`](triagem-bitrix/docs/remediacao/ARQUITETURA-CORRIGIDA.md) ·
[`PLANO-IMPLANTACAO.md`](triagem-bitrix/docs/remediacao/PLANO-IMPLANTACAO.md).

```bash
cd triagem-bitrix
python3 -m venv .venv && .venv/bin/pip install pytest
.venv/bin/python -m pytest -q
```

> **Dados:** esta é uma cópia de portfólio. Nomes de clientes, CPFs, identificadores de
> chats, planilha, domínio e credenciais do escritório foram substituídos por valores
> fictícios (os CPFs de teste são sintéticos, com dígito verificador válido). Nenhuma
> credencial real está no código nem no histórico deste repositório.
