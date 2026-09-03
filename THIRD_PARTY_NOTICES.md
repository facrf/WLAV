# Componentes de terceiros

## wa-crypt-tools

O contêiner do WLAV inclui o utilitário independente
[wa-crypt-tools](https://github.com/ElDavoo/wa-crypt-tools), de Davide Palma,
licenciado sob GNU GPL versão 3 ou posterior.

O código MIT do WLAV não incorpora o código-fonte desse componente. A integração
é feita por execução separada do comando `wadecrypt`. O pacote e sua licença são
distribuídos dentro da imagem Python e sua fonte exata está fixada em
`pyproject.toml`.
