# CIAP Gestor de Alternativas Penais

Aplicação web Python/Flask para cadastro e acompanhamento de pessoas em cumprimento de alternativas penais.

## Organização do projeto

- `app.py`: rotas, regras de negócio, persistência e inicialização da aplicação.
- `templates/`: páginas HTML e modelos dos documentos para impressão.
- `static/css/style.css`: identidade visual e estilos responsivos.
- `static/js/app.js`: ponto reservado para interações JavaScript.
- `data/`: banco SQLite criado automaticamente em tempo de execução.
- `documentos/`: armazenamento privado dos documentos enviados pelos usuários; o diretório pode ser movido com `CIAP_DOCUMENTS_DIR`.
- `backup.py`: backup consistente do SQLite e dos documentos, com retenção configurável.
- `requirements.txt`: dependências Python.

## Executar

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Acesse `http://localhost:5000`.

**Acesso inicial local:** `admin@ciap.local` / `admin123`. Essas credenciais são somente para desenvolvimento local. Em produção, configure `CIAP_SECRET`, `CIAP_ADMIN_EMAIL` e `CIAP_ADMIN_PASSWORD` como variáveis secretas antes do primeiro boot. A aplicação rejeita a inicialização se `CIAP_ENV=production` ou `FLASK_ENV=production` estiver ativo sem esses valores ou com as credenciais padrão.

## Recursos

- Login individual de funcionários e sessão protegida.
- Cadastro alinhado ao cabeçalho da planilha fornecida.
- Upload separado dos oito tipos de documentação; certidões dos filhos aceitam múltiplos arquivos.
- Banco SQLite local, filtros por nome/CPF/processo e Grupo de Responsabilização.
- Registros de atendimentos da equipe multidisciplinar.
- Termo de Atendimento Unificado e Formulário de Retorno em versão para impressão pelo navegador.
- Auditoria de cadastros e atendimentos por usuário e data.
- Grupos reflexivos separados por categoria em lotes de até 20 pessoas.
- Exportação da base cadastrada em CSV compatível com planilhas.

## Deploy no Render

O arquivo `render.yaml` configura o serviço web com Gunicorn. A planilha de produção não deve ser commitada nem mantida na raiz pública do projeto. Para uma carga inicial, armazene-a fora do repositório e informe o caminho em `CIAP_IMPORT_FILE` durante uma operação controlada; se o banco estiver vazio e `CIAP_AUTO_IMPORT=1`, a aplicação importa a aba `Dash_Base_Dados`. A importação não é repetida quando já existem assistidos.

No Render gratuito, não use SQLite para dados reais: o filesystem é efêmero. Configure `DATABASE_URL` para um PostgreSQL gerenciado, como Supabase ou Neon, e use um bucket privado S3-compatível, como Cloudflare R2, para os documentos. A aplicação rejeita o boot de produção sem PostgreSQL e sem bucket configurado.

Variáveis de importação:

```text
CIAP_AUTO_IMPORT=1
CIAP_IMPORT_FILE=/caminho/fora-do-repositorio/base.xlsx
CIAP_IMPORT_SHEET=Dash_Base_Dados
```

Não envie dados reais por variáveis de ambiente ou pelo Git. Use um armazenamento privado e remova o arquivo após a carga inicial, quando possível.

Para migrar a instalação local existente, configure `DATABASE_URL` e as variáveis do bucket e execute:

```bash
python migrate_to_postgres.py --sqlite data/ciap.db --documents documentos --upload-documents
```

O destino PostgreSQL deve estar vazio. O script copia as tabelas, preserva os IDs, ajusta as sequências e envia os documentos para `documentos/` no bucket. Faça backup do SQLite antes da migração e valide a quantidade de registros depois.

## Produção

Defina uma chave secreta forte e mantenha dados e documentos fora do diretório público da aplicação quando possível:

```powershell
$env:CIAP_SECRET = "chave-longa-e-aleatoria"
$env:CIAP_HTTPS = "1"
$env:CIAP_SSL_CERT = "C:\certs\ciap.crt"
$env:CIAP_SSL_KEY = "C:\certs\ciap.key"
$env:CIAP_DOCUMENTS_DIR = "D:\DadosCIAP\documentos"
$env:CIAP_BACKUP_DIR = "E:\BackupsCIAP"
$env:CIAP_ADMIN_EMAIL = "ciapcadastro@gmail.com"
$env:CIAP_SMTP_HOST = "smtp.gmail.com"
$env:CIAP_SMTP_PORT = "587"
$env:CIAP_SMTP_USER = "ciapcadastro@gmail.com"
$env:CIAP_SMTP_PASSWORD = "senha-de-app-do-email"
$env:CIAP_SMTP_FROM = "ciapcadastro@gmail.com"
python app.py
```

Para o Render com PostgreSQL e R2/S3, configure também `DATABASE_URL`, `CIAP_S3_BUCKET`, `CIAP_S3_ENDPOINT_URL`, `CIAP_S3_REGION`, `CIAP_S3_ACCESS_KEY_ID` e `CIAP_S3_SECRET_ACCESS_KEY`. O endpoint é obrigatório para R2; para AWS S3, deixe `CIAP_S3_ENDPOINT_URL` vazio. `CIAP_S3_SERVER_SIDE_ENCRYPTION` é opcional e pode ser `AES256` para AWS S3; no R2, deixe vazio. Mantenha `CIAP_AUTO_IMPORT=0` depois da migração.

O cadastro de novos usuários fica pendente até a aprovação na aba **Solicitações**. O envio de e-mails usa SMTP; para Gmail, gere uma senha de aplicativo e não use a senha normal da conta.

HTTPS exige os dois arquivos de certificado. Para uma implantação com proxy reverso, termine o TLS no proxy e mantenha o Flask escutando apenas em `127.0.0.1`.

Para instalações locais com SQLite, execute o backup diariamente pelo Agendador de Tarefas do Windows (ou cron):

```powershell
python backup.py
```

O `backup.py` usa a API de cópia do SQLite, inclui documentos locais e mantém 30 cópias por padrão (`CIAP_BACKUP_RETENTION`); ele não substitui backup PostgreSQL. Em produção com Supabase/Neon, ative os backups e a retenção oferecidos pelo provedor. No R2/S3, ative versionamento do bucket e uma política de retenção. Teste periodicamente a restauração do PostgreSQL e dos documentos e restrinja as permissões das credenciais de serviço.
