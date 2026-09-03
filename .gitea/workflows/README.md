# Workflows desativados no espelho Gitea

Este diretório existe intencionalmente e não deve conter arquivos YAML. Como o
Gitea procura primeiro por `.gitea/workflows`, sua presença impede o fallback
para o workflow de publicação exclusivo do GitHub em `.github/workflows`.

A geração e a publicação da imagem WLAV pertencem somente ao GitHub Actions e ao
registro `ghcr.io/facrf/wlav`.
