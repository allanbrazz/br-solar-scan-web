# Braz Solar Scan

Aplicacao web Django para detectar e analisar falhas em sistemas fotovoltaicos de
microgeracao. O sistema combina telemetria de inversores, dados meteorologicos e
satelitais, modelos de potencia e pipelines FDD (Fault Detection and Diagnosis).

## Componentes

- cadastro de plantas, modulos, inversores, strings e cabeamento;
- integracao com Growatt e Renovigi/ShineMonitor;
- ingestao meteorologica via Open-Meteo e NSRDB;
- consolidacao temporal em 15 minutos;
- deteccao de mismatch, classificacao de eventos e validacao com ground truth;
- diagnostico MPPT com baseline scikit-learn e experimentos GNN opcionais;
- dashboards, heatmaps e exportacao de relatorios PDF.

## Execucao local

```powershell
Copy-Item .env.example .env
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py createsuperuser
.\.venv\Scripts\python.exe manage.py runserver
```

Acesse `http://127.0.0.1:8000/`. Para os experimentos PyTorch, instale
`requirements-ml.txt`.

## Executavel para Windows

O pacote desktop inicia um servidor HTTP local restrito a `127.0.0.1`, aplica as
migracoes e abre o sistema no navegador padrao. Nao depende de Render, PostgreSQL
ou conexao com um servidor externo para as paginas locais. Integracoes como
Renovigi, Open-Meteo e NSRDB continuam exigindo internet quando utilizadas.

Para gerar a versao portatil:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-desktop.txt
.\build_desktop.ps1
```

Arquivos gerados:

- `dist\BrazSolarScan\BrazSolarScan.exe`
- `artifacts\BrazSolarScan-Windows.zip`

O workflow `Executavel Windows` tambem gera esse ZIP no GitHub Actions e o
disponibiliza como artefato para download por 30 dias.

Os dados do cliente ficam persistidos em `%APPDATA%\BrazSolarScan`, separados
dos arquivos do programa. O formato `onedir` foi escolhido para reduzir tempo de
inicializacao e uso de memoria em comparacao com um executavel `onefile`.

## Deploy no Render

O arquivo `render.yaml` cria o servico web e um PostgreSQL. No Blueprint do
Render, informe `DJANGO_SUPERUSER_PASSWORD` e, quando aplicavel, as chaves NREL e
Renovigi. O build coleta os arquivos estaticos, executa as migracoes e cria o
administrador inicial.

Arquivos em disco, incluindo uploads e modelos gerados em runtime, sao efemeros
em instancias sem disco persistente. Para uso alem de demonstracao, configure um
disco persistente ou armazenamento de objetos.

## Seguranca

Nunca versionar `.env` ou credenciais reais. Como o historico anterior continha
um `.env`, as chaves ali presentes devem ser revogadas e substituidas antes de
publicar a aplicacao.
