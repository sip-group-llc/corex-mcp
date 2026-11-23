---
name: corex
description: Criar imagens, vídeos e áudio no CoreX hospedado pelo MCP, com biblioteca, projetos, UGC, Director, cotação e acompanhamento.
---

# CoreX pelo MCP

Use o serviço https://corex.art e o cliente [corex-mcp](https://github.com/sip-group-llc/corex-mcp).
[Instalação e conexão](https://github.com/sip-group-llc/corex-mcp/blob/main/README.md).

O MCP encaminha os trabalhos ao serviço hospedado, incluindo processamento e fornecedores; não exige executar o backend na máquina do usuário.

## Conectar

Clone o cliente, instale-o em ambiente Python 3.11+ com `python -m pip install .` e execute:
```text
corex-mcp configure --upload-root CAMINHO_DA_PASTA_DE_MIDIAS
```
Configure um servidor MCP stdio com o executável `corex-mcp` desse ambiente e o argumento `mcp`.
A credencial é solicitada em terminal privado. Use a integração do próprio workspace. O escopo Studio completo habilita imagem, vídeo e áudio; tokens UGC têm catálogo reduzido.

## Produzir

1. `get_integration_context`: confirme workspace, conta e escopo.
2. `list_studio_tools`: descubra modelos/modalidades e campos atuais. `get_workspace_balance`: confira créditos.
3. Para mídia local autorizada, `upload_file` envia o arquivo da pasta configurada; depois `add_asset` registra a chave na biblioteca. Enviar arquivo não inicia geração.
4. `estimate_studio`: cote exatamente a entrada desejada. `generate_studio`: use a mesma entrada, teto autorizado e chave de idempotência.
5. `get_job`: acompanhe até estado terminal. Use `list_jobs`, biblioteca e projetos para retomar resultados.
6. UGC e Director têm ferramentas próprias no catálogo; leia o schema do fluxo antes de chamar.
7. Genjutsu (trocar pessoa, trocar objeto ou mudar o estilo de um vídeo, mantendo movimento e câmera): `list_genjutsu_library` mostra presets com original, resultado e prompt; `use_genjutsu_preset` copia o escolhido para o workspace sem gastar créditos e devolve `studioRequest` (`toolId` e `input`). Complete o que estiver em `pendente` (a pessoa ou o produto) e siga pelo passo 4. Sem preset, use direto `face-swap-video`, `object-swap-video` ou `restyle-video`.

O cliente publica 41 ferramentas de workspace e uma de upload; a lista efetiva depende do token. Cota, modelos e disponibilidade vêm do serviço atual. Upload local suporta até 500 MiB, dentro da pasta permitida. Não amplie a pasta para contornar uma recusa.

Após timeout, consulte o job antes de repetir. Respeite a autorização de gasto e não declare sucesso antes de obter o resultado final.
