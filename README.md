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

**Acesso inicial:** `admin@ciap.local` / `admin123`. Altere a senha e a variável `CIAP_SECRET` antes de uso institucional.

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

O cadastro de novos usuários fica pendente até a aprovação na aba **Solicitações**. O envio de e-mails usa SMTP; para Gmail, gere uma senha de aplicativo e não use a senha normal da conta.

HTTPS exige os dois arquivos de certificado. Para uma implantação com proxy reverso, termine o TLS no proxy e mantenha o Flask escutando apenas em `127.0.0.1`.

Execute o backup diariamente pelo Agendador de Tarefas do Windows (ou cron):

```powershell
python backup.py
```

O backup usa a API de cópia do SQLite, inclui os documentos privados e mantém 30 cópias por padrão (`CIAP_BACKUP_RETENTION`). Teste periodicamente a restauração e restrinja as permissões das pastas de dados e backup à conta do serviço.
