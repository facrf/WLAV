# Workflows desativados no espelho Forgejo

Este diretório existe intencionalmente e não deve conter arquivos YAML. Como o
Forgejo procura `.github/workflows` quando `.forgejo/workflows` não existe, este
arquivo impede o fallback para o workflow de publicação exclusivo do GitHub.

A geração e a publicação da imagem WLAV pertencem somente ao GitHub Actions e ao
registro `ghcr.io/facrf/wlav`.
