import csv
import io
import json
import os
import re
import secrets
import smtplib
import sqlite3
import hmac
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

from flask import (
    Flask,
    Response,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

# 1. Instância principal da aplicação
app = Flask(__name__)
app.secret_key = os.environ.get("CIAP_SECRET", "ciap-dev-secret-change-me")
if os.environ.get("CIAP_ENV") == "production" and app.secret_key == "ciap-dev-secret-change-me":
    raise RuntimeError("CIAP_SECRET deve ser configurada em produção")

# 2. Definição dos diretórios locais e caminho do banco legado (DB)
BASE = Path(__file__).parent
DB = Path(os.environ.get("CIAP_DB_PATH", BASE / "data" / "ciap.db")).resolve()
UPLOADS = Path(os.environ.get("CIAP_DOCUMENTS_DIR", BASE / "documentos")).resolve()

UPLOADS.mkdir(parents=True, exist_ok=True)
DB.parent.mkdir(parents=True, exist_ok=True)

# 3. Configuração dinâmica do Banco de Dados (Render / SQLite Local)
db_url = os.getenv("DATABASE_URL", f"sqlite:///{DB}")

if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# 4. Configurações extras de segurança e upload
app.config.update(
    MAX_CONTENT_LENGTH=int(os.environ.get("CIAP_MAX_UPLOAD_BYTES", 25 * 1024 * 1024)),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("CIAP_HTTPS", "0") == "1",
)

# 5. Inicialização do SQLAlchemy com o app já configurado
db = SQLAlchemy(app)
ADMIN_EMAIL = os.environ.get("CIAP_ADMIN_EMAIL", "admin@ciap.local")
ADMIN_PASSWORD = os.environ.get("CIAP_ADMIN_PASSWORD", "admin123")
if os.environ.get("CIAP_ENV") == "production":
    if not os.environ.get("CIAP_ADMIN_EMAIL"):
        raise RuntimeError("CIAP_ADMIN_EMAIL deve ser configurado em produção")
    if not os.environ.get("CIAP_ADMIN_PASSWORD"):
        raise RuntimeError("CIAP_ADMIN_PASSWORD deve ser configurado em produção")
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PASSWORD_PATTERN = re.compile(r"^[A-Za-z0-9]{6,}$")


@app.after_request
def disable_response_cache(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


DOCS = [
    "Termo de Audiência",
    "Identificação Pessoal (RG, CPF, CNH ou outro documento com foto)",
    "Comprovante de escolaridade",
    "Certidão de nascimento, casamento ou divórcio",
    "Certidão de Nascimento dos filhos (menores de idade)",
    "Comprovante de endereço atual (últimos 3 meses)",
    "Comprovante do atual trabalho ou profissão",
    "Carteira de Trabalho",
    "Cartão CNPJ (se empresário)",
    "Cartão do SUS",
    "Número do Cadastro Único (se recebe benefícios sociais)",
]

FIELDS = [
    "nome", "nome_social", "cpf", "rg", "data_nascimento", "nome_mae", "processo", "vara", "rji",
    "telefone", "data_atendimento", "raca", "sexo", "identidade_genero", "orientacao_sexual",
    "escolaridade", "pcd", "tipo_deficiencia", "nacionalidade", "pais", "ocupacao", "profissao",
    "religiao", "diploma_legal", "artigo", "tipo_penal", "grupamento_penal", "natureza_atendimento",
    "medida", "grupo_responsabilizacao", "status", "atendimento_individual", "comparecimento",
    "termino_medida", "situacao_final", "observacoes",
]
GROUP_STATUS_OPTIONS = ["Em andamento", "Concluiu", "Desistiu", "Eliminado - refazer grupo"]
FREQUENCY_STATUS_OPTIONS = ["", "Presente", "Faltou"]

LABELS = dict(zip(FIELDS, [
    "Nome do Assistido(a)", "Nome Social", "CPF", "RG/órgão emissor", "Data de nascimento", "Nome da mãe",
    "Número do Processo", "Órgão Judicial/Vara", "Nº Inscrição (ID único)", "Telefone(s) WhatsApp",
    "Data de Atendimento", "Raça/cor da pele (Declarada)", "Sexo (biológico)", "Identidade de Gênero (Declarada)",
    "Orientação Sexual (autodeclaração)", "Escolaridade", "Pessoa com Deficiência", "Tipo de Deficiência",
    "Nacionalidade", "País", "Ocupação", "Profissão", "Religião", "Diploma Legal", "Artigo/Capitulação",
    "Tipo Penal", "Grupamento Penal", "Natureza do Atendimento", "Tipo de Medida/Alternativa",
    "Grupo de Responsabilização", "Status do Atendimento", "Atendimento Individual", "Comparecimento Voluntário",
    "Data de Término da Medida", "Situação Final", "Observação/Justificativas",
]))

ATTENDANCE_FIELDS = [
    "data", "inicio_fim", "profissional", "tipo_retorno", "compareceu", "busca_ativa", "emprego",
    "aderencia", "mudou_contato", "relato", "intervencao", "pendencias", "conclusao", "recomendacao",
]
ATTENDANCE_LABELS = dict(zip(ATTENDANCE_FIELDS, [
    "Data do Atendimento", "Horário de início/Fim", "Profissional responsável (Nome e Cargo)", "Tipo de Retorno",
    "Comparecimento voluntário (Sim/Não)", "Convocação/busca ativa (Sim/Não)", "Empregado ou estudando (Sim/Não)",
    "Aderência/compreensão das medidas (Sim/Não)", "Mudança de endereço/contato (Sim/Não)", "Relato subjetivo/observação",
    "Intervenção do profissional da CIAP", "Encaminhamentos pendentes/próximos passos", "Conclusão: Regular ou Irregular/Risco",
    "Recomendação ao Juízo",
]))
PROFESSIONAL_OPTIONS = ["Assistente Jurídico", "Assistente Social", "Psicólogo"]
MONTH_DAY_OPTIONS = [(day, str(day)) for day in range(1, 32)]


def db():
    connection = sqlite3.connect(DB)
    connection.row_factory = sqlite3.Row
    return connection


def init_db():
    connection = db()
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY, nome TEXT, email TEXT UNIQUE, senha TEXT,
            cargo TEXT, perfil TEXT DEFAULT 'profissional', status TEXT DEFAULT 'pendente',
            ativo INTEGER DEFAULT 1, criado_em TEXT DEFAULT '', aprovado_em TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS pessoas (
            id INTEGER PRIMARY KEY, criado_em TEXT, criado_por INTEGER,
            documentos TEXT DEFAULT "", %s
        );
        CREATE TABLE IF NOT EXISTS atendimentos (
            id INTEGER PRIMARY KEY, pessoa_id INTEGER, %s,
            criado_por INTEGER, criado_em TEXT
        );
        CREATE TABLE IF NOT EXISTS agendamentos (
            id INTEGER PRIMARY KEY, pessoa_id INTEGER NOT NULL,
            profissional TEXT NOT NULL, data TEXT NOT NULL, hora TEXT NOT NULL,
            observacao TEXT DEFAULT "", observacao_falta TEXT DEFAULT "", status TEXT DEFAULT 'Agendado',
            criado_por INTEGER, criado_em TEXT
        );
        CREATE TABLE IF NOT EXISTS alertas_agendamento (
            id INTEGER PRIMARY KEY, pessoa_id INTEGER NOT NULL,
            profissional TEXT NOT NULL, data_preferencial TEXT DEFAULT "",
            hora_preferencial TEXT DEFAULT "", observacao TEXT DEFAULT "",
            status TEXT DEFAULT 'Aguardando vaga', criado_por INTEGER, criado_em TEXT
        );
        CREATE TABLE IF NOT EXISTS disponibilidades (
            id INTEGER PRIMARY KEY, profissional TEXT NOT NULL,
            dia_mes INTEGER NOT NULL, hora_inicio TEXT NOT NULL, hora_fim TEXT NOT NULL,
            criado_por INTEGER, criado_em TEXT
        );
        CREATE TABLE IF NOT EXISTS frequencias (
            id INTEGER PRIMARY KEY, pessoa_id INTEGER NOT NULL,
            encontro INTEGER NOT NULL, status TEXT DEFAULT "", data TEXT DEFAULT "",
            UNIQUE(pessoa_id, encontro)
        );
        CREATE TABLE IF NOT EXISTS auditoria (
            id INTEGER PRIMARY KEY, usuario_id INTEGER, acao TEXT, entidade TEXT,
            entidade_id INTEGER, criado_em TEXT
        );
        """ % (
            ", ".join(f"{field} TEXT" for field in FIELDS),
            ", ".join(f"{field} TEXT" for field in ATTENDANCE_FIELDS),
        )
    )
    columns = {row[1] for row in connection.execute("PRAGMA table_info(pessoas)")}
    for field in ("situacao_grupo", "alerta_frequencia"):
        if field not in columns:
            connection.execute(f'ALTER TABLE pessoas ADD COLUMN {field} TEXT DEFAULT ""')
    availability_columns = {row[1] for row in connection.execute("PRAGMA table_info(disponibilidades)")}
    if "dia_mes" not in availability_columns:
        connection.execute("ALTER TABLE disponibilidades ADD COLUMN dia_mes INTEGER")
    appointment_columns = {row[1] for row in connection.execute("PRAGMA table_info(agendamentos)")}
    if "status" not in appointment_columns:
        connection.execute("ALTER TABLE agendamentos ADD COLUMN status TEXT DEFAULT 'Agendado'")
    if "observacao_falta" not in appointment_columns:
        connection.execute('ALTER TABLE agendamentos ADD COLUMN observacao_falta TEXT DEFAULT ""')
    user_columns = {row[1] for row in connection.execute("PRAGMA table_info(users)")}
    if "perfil" not in user_columns:
        connection.execute("ALTER TABLE users ADD COLUMN perfil TEXT DEFAULT 'profissional'")
    if "status" not in user_columns:
        connection.execute("ALTER TABLE users ADD COLUMN status TEXT DEFAULT 'aprovado'")
    if "criado_em" not in user_columns:
        connection.execute("ALTER TABLE users ADD COLUMN criado_em TEXT DEFAULT ''")
    if "aprovado_em" not in user_columns:
        connection.execute("ALTER TABLE users ADD COLUMN aprovado_em TEXT DEFAULT ''")
    connection.execute(
        "UPDATE users SET perfil = 'administrador', status = 'aprovado' "
        "WHERE cargo = 'Administrador'"
    )
    connection.execute(
        "UPDATE pessoas SET alerta_frequencia = CASE "
        "WHEN (SELECT COUNT(*) FROM frequencias f WHERE f.pessoa_id = pessoas.id AND f.status = 'Faltou') >= 3 "
        "THEN 'ALERTA: 3 ou mais faltas. Assistido eliminado e deve refazer o grupo.' "
        "WHEN (SELECT COUNT(*) FROM frequencias f WHERE f.pessoa_id = pessoas.id AND f.status = 'Faltou') = 2 "
        "THEN 'ALERTA: 2 faltas registradas. Na próxima falta, o Assistido será eliminado e deverá refazer o grupo.' "
        "ELSE '' END"
    )
    if not connection.execute("SELECT 1 FROM users LIMIT 1").fetchone():
        connection.execute(
            "INSERT INTO users(nome, email, senha, cargo, perfil, status, criado_em, aprovado_em) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("Administrador", ADMIN_EMAIL, generate_password_hash(ADMIN_PASSWORD), "Administrador",
             "administrador", "aprovado", datetime.now().isoformat(timespec="seconds"),
             datetime.now().isoformat(timespec="seconds")),
        )
    connection.commit()
    connection.close()


def bootstrap_planilha():
    if os.environ.get("CIAP_AUTO_IMPORT", "1") != "1":
        return
    spreadsheet = Path(os.environ.get(
        "CIAP_IMPORT_FILE", BASE / "BASE_ATENDIMENTOS_CIAP_2026_COMPLETA (Recuperado).xlsx"
    )).resolve()
    if not spreadsheet.is_file():
        app.logger.info("Planilha de importação não encontrada: %s", spreadsheet)
        return
    connection = db()
    people_count = connection.execute("SELECT COUNT(*) FROM pessoas").fetchone()[0]
    connection.close()
    if people_count:
        return
    from import_planilha import import_rows

    sheet_name = os.environ.get("CIAP_IMPORT_SHEET", "Dash_Base_Dados")
    inserted, empty, duplicate = import_rows(spreadsheet, sheet_name)
    app.logger.info(
        "Importação inicial concluída: inseridos=%s vazios=%s duplicados=%s",
        inserted, empty, duplicate,
    )


def audit(action, entity, entity_id=None):
    connection = db()
    connection.execute(
        "INSERT INTO auditoria(usuario_id, acao, entidade, entidade_id, criado_em) VALUES (?, ?, ?, ?, ?)",
        (session["uid"], action, entity, entity_id, datetime.now().isoformat(timespec="seconds")),
    )
    connection.commit()
    connection.close()


def authenticated():
    return "uid" in session and session.get("status") == "aprovado"


def csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def normalize_identifier(value):
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def find_duplicate_person(connection, values, exclude_id=None):
    identifiers = {
        field: normalize_identifier(values[FIELDS.index(field)])
        for field in ("cpf", "processo", "rji")
    }
    identifiers = {field: value for field, value in identifiers.items() if value}
    if not identifiers:
        return None
    query = "SELECT id, cpf, processo, rji FROM pessoas"
    parameters = []
    if exclude_id is not None:
        query += " WHERE id <> ?"
        parameters.append(exclude_id)
    for person in connection.execute(query, parameters):
        for field, value in identifiers.items():
            if normalize_identifier(person[field]) == value:
                return field.upper()
    return None


def upload_filename(person_id, document_index, original_name):
    safe_name = secure_filename(original_name)
    if not safe_name:
        raise ValueError("Nome de arquivo inválido")
    return f"{person_id}_{document_index}_{secrets.token_hex(8)}_{safe_name}"


@app.before_request
def protect_state_changes():
    if request.method == "POST":
        submitted = request.form.get("_csrf_token", "")
        expected = session.get("csrf_token", "")
        if not expected or not submitted or not hmac.compare_digest(submitted, expected):
            return "Token de segurança inválido. Recarregue a página e tente novamente.", 400


def is_admin():
    return authenticated() and session.get("perfil") == "administrador"


def admin_required():
    if not authenticated():
        return redirect(url_for("login"))
    if not is_admin():
        return "Acesso permitido apenas para administradores.", 403
    return None


def send_email(recipient, subject, body):
    smtp_host = os.environ.get("CIAP_SMTP_HOST")
    smtp_port = int(os.environ.get("CIAP_SMTP_PORT", "587"))
    smtp_user = os.environ.get("CIAP_SMTP_USER")
    smtp_password = os.environ.get("CIAP_SMTP_PASSWORD")
    if not all((smtp_host, smtp_user, smtp_password)):
        app.logger.warning("E-mail não enviado: configure CIAP_SMTP_HOST, CIAP_SMTP_USER e CIAP_SMTP_PASSWORD")
        return False
    message = EmailMessage()
    message["From"] = os.environ.get("CIAP_SMTP_FROM", smtp_user)
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)
    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
            server.starttls()
            server.login(smtp_user, smtp_password)
            server.send_message(message)
    except (OSError, smtplib.SMTPException):
        app.logger.exception("Falha ao enviar e-mail para %s", recipient)
        return False
    return True


def login_required():
    return authenticated() or redirect(url_for("login"))


@app.context_processor
def template_context():
    return {
        "labels": LABELS, "fields": FIELDS, "docs": DOCS,
        "group_status_options": GROUP_STATUS_OPTIONS,
        "frequency_status_options": FREQUENCY_STATUS_OPTIONS,
        "professional_options": PROFESSIONAL_OPTIONS,
        "month_day_options": MONTH_DAY_OPTIONS,
        "is_admin": is_admin(),
        "current_user": session.get("nome", ""),
        "csrf_token": csrf_token(),
        "static_version": int(max(
            (BASE / "static" / "css" / "style.css").stat().st_mtime,
            (BASE / "static" / "js" / "app.js").stat().st_mtime,
        )),
    }


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        connection = db()
        user = connection.execute(
            "SELECT * FROM users WHERE lower(email) = lower(?) AND ativo = 1 AND status = 'aprovado'",
            (request.form["email"].strip(),),
        ).fetchone()
        connection.close()
        if user and check_password_hash(user["senha"], request.form["senha"]):
            session.update(uid=user["id"], nome=user["nome"], perfil=user["perfil"], status=user["status"])
            return redirect(url_for("dashboard"))
        flash("E-mail ou senha inválidos.")
    return render_template("login.html", title="Login")


@app.route("/cadastro", methods=["GET", "POST"])
def cadastro():
    if request.method == "POST":
        name = request.form.get("nome", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("senha", "")
        confirmation = request.form.get("confirmacao", "")
        if not name or not EMAIL_PATTERN.fullmatch(email):
            flash("Informe um nome e um e-mail válido.")
            return render_template("cadastro.html", title="Solicitar acesso")
        if not PASSWORD_PATTERN.fullmatch(password):
            flash("A senha deve ter no mínimo 6 caracteres, usando apenas letras e números.")
            return render_template("cadastro.html", title="Solicitar acesso")
        if password != confirmation:
            flash("A confirmação da senha não confere.")
            return render_template("cadastro.html", title="Solicitar acesso")
        connection = db()
        try:
            connection.execute(
                "INSERT INTO users(nome, email, senha, cargo, perfil, status, ativo, criado_em) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (name, email, generate_password_hash(password), "Profissional", "profissional",
                 "pendente", 0, datetime.now().isoformat(timespec="seconds")),
            )
            connection.commit()
        except sqlite3.IntegrityError:
            connection.close()
            flash("Este e-mail já possui cadastro ou solicitação.")
            return render_template("cadastro.html", title="Solicitar acesso")
        connection.close()
        send_email(
            email,
            "Solicitação de acesso recebida - CIAP",
            f"Olá, {name}.\n\nSua solicitação de acesso ao CIAP foi recebida e aguarda validação do administrador.\n\nVocê receberá um novo e-mail quando o perfil for aprovado.",
        )
        send_email(
            ADMIN_EMAIL,
            "Nova solicitação de acesso - CIAP",
            f"Uma nova solicitação de acesso foi criada.\n\nNome: {name}\nE-mail: {email}\n\nAcesse o painel administrativo para validar o cadastro.",
        )
        flash("Solicitação enviada. Aguarde a validação do administrador.")
        return redirect(url_for("login"))
    return render_template("cadastro.html", title="Solicitar acesso")


@app.route("/perfis")
@app.route("/solicitacoes")
def perfis():
    denial = admin_required()
    if denial:
        return denial
    connection = db()
    requests = connection.execute(
        "SELECT * FROM users WHERE status = 'pendente' ORDER BY criado_em, id"
    ).fetchall()
    users = connection.execute(
        "SELECT * FROM users WHERE status = 'aprovado' ORDER BY nome"
    ).fetchall()
    connection.close()
    return render_template("solicitacoes.html", title="Perfis", requests=requests, users=users)


@app.route("/perfis/<int:user_id>/editar", methods=["GET", "POST"])
def editar_perfil(user_id):
    denial = admin_required()
    if denial:
        return denial
    connection = db()
    user = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user:
        connection.close()
        return "Usuário não encontrado", 404
    if request.method == "POST":
        name = request.form.get("nome", "").strip()
        email = request.form.get("email", "").strip().lower()
        perfil = request.form.get("perfil", "")
        status = request.form.get("status", "")
        password = request.form.get("senha", "")
        if not name or not EMAIL_PATTERN.fullmatch(email):
            flash("Informe um nome e um e-mail válido.")
        elif perfil not in ("profissional", "administrador") or status not in ("pendente", "aprovado", "rejeitado"):
            flash("Perfil ou status inválido.")
        elif password and not PASSWORD_PATTERN.fullmatch(password):
            flash("A senha deve ter no mínimo 6 caracteres, usando apenas letras e números.")
        else:
            try:
                fields = ["nome = ?", "email = ?", "perfil = ?", "cargo = ?", "status = ?", "ativo = ?"]
                values = [name, email, perfil, "Administrador" if perfil == "administrador" else "Profissional",
                          status, 1 if status == "aprovado" else 0]
                if password:
                    fields.append("senha = ?")
                    values.append(generate_password_hash(password))
                values.append(user_id)
                connection.execute("UPDATE users SET %s WHERE id = ?" % ", ".join(fields), values)
                connection.commit()
            except sqlite3.IntegrityError:
                flash("Este e-mail já está sendo usado por outro usuário.")
            else:
                connection.close()
                audit("Perfil de usuário atualizado", "usuario", user_id)
                flash("Perfil atualizado com sucesso.")
                return redirect(url_for("perfis"))
    connection.close()
    return render_template("perfil_form.html", title="Editar perfil", user=user)


@app.route("/solicitacoes/<int:user_id>/aprovar", methods=["POST"])
def aprovar_solicitacao(user_id):
    denial = admin_required()
    if denial:
        return denial
    perfil = request.form.get("perfil", "profissional")
    if perfil not in ("profissional", "administrador"):
        flash("Perfil inválido.")
        return redirect(url_for("perfis"))
    connection = db()
    user = connection.execute("SELECT * FROM users WHERE id = ? AND status = 'pendente'", (user_id,)).fetchone()
    if not user:
        connection.close()
        return "Solicitação não encontrada", 404
    now = datetime.now().isoformat(timespec="seconds")
    connection.execute(
        "UPDATE users SET perfil = ?, cargo = ?, status = 'aprovado', ativo = 1, aprovado_em = ? WHERE id = ?",
        (perfil, "Administrador" if perfil == "administrador" else "Profissional", now, user_id),
    )
    connection.commit()
    connection.close()
    send_email(
        user["email"],
        "Acesso aprovado - CIAP",
        f"Olá, {user['nome']}.\n\nSeu acesso ao CIAP foi aprovado. Você já pode entrar com o e-mail cadastrado e a senha criada na solicitação.",
    )
    audit("Solicitação de acesso aprovada", "usuario", user_id)
    flash("Usuário aprovado e notificado por e-mail.")
    return redirect(url_for("perfis"))


@app.route("/solicitacoes/<int:user_id>/rejeitar", methods=["POST"])
def rejeitar_solicitacao(user_id):
    denial = admin_required()
    if denial:
        return denial
    connection = db()
    user = connection.execute("SELECT email FROM users WHERE id = ? AND status = 'pendente'", (user_id,)).fetchone()
    if not user:
        connection.close()
        return "Solicitação não encontrada", 404
    connection.execute("UPDATE users SET status = 'rejeitado', ativo = 0 WHERE id = ?", (user_id,))
    connection.commit()
    connection.close()
    send_email(user["email"], "Solicitação de acesso - CIAP", "Sua solicitação de acesso ao CIAP não foi aprovada.")
    audit("Solicitação de acesso rejeitada", "usuario", user_id)
    flash("Solicitação rejeitada e usuário notificado por e-mail.")
    return redirect(url_for("perfis"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/relatorios")
def relatorios():
    if not authenticated():
        return redirect(url_for("login"))
    start_date = request.args.get("inicio", "")
    end_date = request.args.get("fim", "")
    professional = request.args.get("profissional", "")
    note_filter = request.args.get("justificativa", "todos")
    conditions = ["a.status = 'Faltou'"]
    parameters = []
    if start_date:
        conditions.append("a.data >= ?")
        parameters.append(start_date)
    if end_date:
        conditions.append("a.data <= ?")
        parameters.append(end_date)
    if professional:
        conditions.append("a.profissional = ?")
        parameters.append(professional)
    if note_filter == "com_relato":
        conditions.append("TRIM(COALESCE(a.observacao_falta, '')) <> ''")
    elif note_filter == "sem_relato":
        conditions.append("TRIM(COALESCE(a.observacao_falta, '')) = ''")
    connection = db()
    absences = connection.execute(
        "SELECT a.*, p.nome, p.cpf, p.processo, p.telefone "
        "FROM agendamentos a JOIN pessoas p ON p.id = a.pessoa_id "
        f"WHERE {' AND '.join(conditions)} ORDER BY a.data DESC, a.hora DESC",
        parameters,
    ).fetchall()
    professionals = connection.execute(
        "SELECT DISTINCT profissional FROM agendamentos WHERE profissional <> '' ORDER BY profissional"
    ).fetchall()
    connection.close()
    with_note = sum(bool((item["observacao_falta"] or "").strip()) for item in absences)
    without_note = len(absences) - with_note
    by_professional = {}
    for item in absences:
        by_professional[item["profissional"]] = by_professional.get(item["profissional"], 0) + 1
    recurring_people = {}
    for item in absences:
        recurring_people[item["pessoa_id"]] = recurring_people.get(item["pessoa_id"], 0) + 1
    recurring_count = sum(total > 1 for total in recurring_people.values())
    report_rows = []
    for item in absences:
        row = dict(item)
        has_note = bool((item["observacao_falta"] or "").strip())
        recurrence = recurring_people[item["pessoa_id"]]
        row["classificacao"] = "Relato registrado" if has_note else "Sem justificativa registrada"
        row["prioridade"] = "Alta" if not has_note or recurrence > 1 else "Acompanhar"
        row["recorrente"] = recurrence > 1
        report_rows.append(row)
    report_stats = {
        "total": len(absences),
        "with_note": with_note,
        "without_note": without_note,
        "recurring_people": recurring_count,
        "top_professional": max(by_professional, key=by_professional.get) if by_professional else "Não informado",
    }
    return render_template(
        "relatorios.html", title="Relatórios", absences=report_rows, professionals=professionals,
        report_stats=report_stats, by_professional=sorted(by_professional.items(), key=lambda item: (-item[1], item[0])),
        filters={"inicio": start_date, "fim": end_date, "profissional": professional, "justificativa": note_filter},
    )


@app.route("/")
def dashboard():
    if not authenticated():
        return redirect(url_for("login"))
    connection = db()
    total = connection.execute("SELECT COUNT(*) n FROM pessoas").fetchone()["n"]
    attendances = connection.execute("SELECT COUNT(*) n FROM atendimentos").fetchone()["n"]
    groups_count = connection.execute(
        "SELECT COUNT(DISTINCT grupo_responsabilizacao) n FROM pessoas WHERE grupo_responsabilizacao <> ''"
    ).fetchone()["n"]
    people = connection.execute("SELECT * FROM pessoas ORDER BY id DESC LIMIT 8").fetchall()
    chart_people = connection.execute(
        "SELECT grupo_responsabilizacao, situacao_grupo, medida, status, natureza_atendimento, "
        "grupamento_penal, sexo, raca, escolaridade FROM pessoas"
    ).fetchall()
    chart_data = {}
    chart_fields = {
        "situacao_grupo": "Situação do grupo",
        "grupo_responsabilizacao": "Grupo de responsabilização",
        "medida": "Tipo de medida",
        "status": "Status do atendimento",
        "natureza_atendimento": "Natureza do atendimento",
        "grupamento_penal": "Grupamento penal",
        "sexo": "Sexo",
        "raca": "Raça/cor",
        "escolaridade": "Escolaridade",
    }
    for field in chart_fields:
        counts = {}
        for person in chart_people:
            value = (person[field] or "").strip() or "Não informado"
            counts[value] = counts.get(value, 0) + 1
        chart_data[field] = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:12]
    frequency_counts = connection.execute(
        "SELECT CASE WHEN status = 'Faltou' THEN 'Faltou' ELSE 'Presente' END AS categoria, COUNT(*) AS total "
        "FROM frequencias WHERE status <> '' GROUP BY categoria ORDER BY categoria"
    ).fetchall()
    chart_data["frequencia"] = [(row["categoria"], row["total"]) for row in frequency_counts]
    connection.close()
    return render_template(
        "dashboard.html", total=total, attendances=attendances, groups_count=groups_count,
        people=people, chart_data=json.dumps(chart_data, ensure_ascii=False), chart_fields=chart_fields,
    )


@app.route("/pessoas")
def pessoas():
    if not authenticated():
        return redirect(url_for("login"))
    query = request.args.get("q", "")
    group = request.args.get("grupo", "")
    connection = db()
    people = connection.execute(
        "SELECT * FROM pessoas WHERE (nome LIKE ? OR cpf LIKE ? OR processo LIKE ?) "
        "AND grupo_responsabilizacao LIKE ? ORDER BY nome",
        (f"%{query}%", f"%{query}%", f"%{query}%", f"%{group}%"),
    ).fetchall()
    groups = connection.execute(
        "SELECT DISTINCT grupo_responsabilizacao FROM pessoas "
        "WHERE grupo_responsabilizacao <> '' ORDER BY 1"
    ).fetchall()
    connection.close()
    return render_template("pessoas.html", people=people, groups=groups, query=query, group=group)


@app.route("/pessoa/nova", methods=["GET", "POST"])
def nova():
    denial = admin_required()
    if denial:
        return denial
    if request.method == "POST":
        connection = db()
        values = [request.form.get(field, "") for field in FIELDS]
        duplicate = find_duplicate_person(connection, values)
        if duplicate:
            connection.close()
            flash(f"Cadastro duplicado: o identificador {duplicate} já pertence a outro assistido.")
            return render_template("pessoa_form.html", title="Novo cadastro")
        cursor = connection.execute(
            "INSERT INTO pessoas(criado_em, criado_por, %s) VALUES (?, ?, %s)" % (
                ", ".join(FIELDS), ", ".join("?" for _ in FIELDS)
            ),
            (datetime.now().isoformat(timespec="seconds"), session["uid"], *values),
        )
        person_id = cursor.lastrowid
        for index, document_label in enumerate(DOCS):
            names = []
            for uploaded in request.files.getlist(f"doc_{index}"):
                if uploaded and uploaded.filename:
                    filename = upload_filename(person_id, index, uploaded.filename)
                    uploaded.save(UPLOADS / filename)
                    names.append(filename)
            if names:
                connection.execute(
                    "UPDATE pessoas SET documentos = COALESCE(documentos, '') || ? WHERE id = ?",
                    (f"{document_label}: {', '.join(names)}\n", person_id),
                )
        connection.commit()
        connection.close()
        audit("Cadastro criado", "pessoa", person_id)
        flash("Cadastro salvo com sucesso.")
        return redirect(url_for("pessoa", pid=person_id))
    return render_template("pessoa_form.html", title="Novo cadastro")
@app.route("/pessoa/<int:pid>/editar", methods=["GET", "POST"])
def editar_pessoa(pid):
    denial = admin_required()
    if denial:
        return denial
    connection = db()
    person = connection.execute("SELECT * FROM pessoas WHERE id = ?", (pid,)).fetchone()
    if not person:
        connection.close()
        return "Não encontrado", 404
    if request.method == "POST":
        values = [request.form.get(field, "") for field in FIELDS]
        duplicate = find_duplicate_person(connection, values, exclude_id=pid)
        if duplicate:
            connection.close()
            flash(f"Cadastro duplicado: o identificador {duplicate} já pertence a outro assistido.")
            return render_template("pessoa_form.html", title="Editar cadastro", person=person)
        connection.execute(
            "UPDATE pessoas SET %s WHERE id = ?" % ", ".join(f"{field} = ?" for field in FIELDS),
            (*values, pid),
        )
        for index, document_label in enumerate(DOCS):
            names = []
            for uploaded in request.files.getlist(f"doc_{index}"):
                if uploaded and uploaded.filename:
                    filename = upload_filename(pid, index, uploaded.filename)
                    uploaded.save(UPLOADS / filename)
                    names.append(filename)
            if names:
                connection.execute(
                    "UPDATE pessoas SET documentos = COALESCE(documentos, '') || ? WHERE id = ?",
                    (f"{document_label}: {', '.join(names)}\n", pid),
                )
        connection.commit()
        connection.close()
        audit("Cadastro atualizado", "pessoa", pid)
        flash("Cadastro atualizado com sucesso.")
        return redirect(url_for("pessoa", pid=pid))
    connection.close()
    return render_template("pessoa_form.html", title="Editar cadastro", person=person)


@app.route("/pessoa/<int:pid>/frequencia", methods=["GET", "POST"])
def editar_frequencia(pid):
    denial = admin_required()
    if denial:
        return denial
    connection = db()
    person = connection.execute("SELECT * FROM pessoas WHERE id = ?", (pid,)).fetchone()
    if not person:
        connection.close()
        return "Não encontrado", 404
    if request.method == "POST":
        frequencies = [
            (index, request.form.get(f"frequencia_status_{index}", ""), request.form.get(f"frequencia_data_{index}", ""))
            for index in range(1, 11)
        ]
        absences = sum(status == "Faltou" for _, status, _ in frequencies)
        if absences >= 3:
            alert = "ALERTA: 3 ou mais faltas. Assistido eliminado e deve refazer o grupo."
            group_status = "Eliminado - refazer grupo"
        elif absences == 2:
            alert = "ALERTA: 2 faltas registradas. Na próxima falta, o Assistido será eliminado e deverá refazer o grupo."
            group_status = request.form.get("situacao_grupo", "Em andamento")
        else:
            alert = ""
            group_status = request.form.get("situacao_grupo", "Em andamento")
        connection.execute(
            "UPDATE pessoas SET situacao_grupo = ?, alerta_frequencia = ? WHERE id = ?",
            (group_status, alert, pid),
        )
        for encontro, status, data in frequencies:
            connection.execute(
                "INSERT INTO frequencias(pessoa_id, encontro, status, data) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(pessoa_id, encontro) DO UPDATE SET status = excluded.status, data = excluded.data",
                (pid, encontro, status, data),
            )
        connection.commit()
        connection.close()
        audit("Frequência atualizada", "pessoa", pid)
        flash("Frequência atualizada com sucesso.")
        return redirect(url_for("pessoa", pid=pid))
    frequencies = {
        row["encontro"]: row for row in connection.execute(
            "SELECT encontro, status, data FROM frequencias WHERE pessoa_id = ? ORDER BY encontro", (pid,)
        )
    }
    connection.close()
    return render_template("frequencia_form.html", person=person, frequencies=frequencies)


@app.route("/pessoa/<int:pid>")
def pessoa(pid):
    if not authenticated():
        return redirect(url_for("login"))
    connection = db()
    person = connection.execute("SELECT * FROM pessoas WHERE id = ?", (pid,)).fetchone()
    attendances = connection.execute(
        "SELECT * FROM atendimentos WHERE pessoa_id = ? ORDER BY id DESC", (pid,)
    ).fetchall()
    frequencies = connection.execute(
        "SELECT encontro, status, data FROM frequencias WHERE pessoa_id = ? ORDER BY encontro", (pid,)
    ).fetchall()
    appointments = connection.execute(
        "SELECT * FROM agendamentos WHERE pessoa_id = ? ORDER BY data, hora", (pid,)
    ).fetchall()
    scheduling_alerts = connection.execute(
        "SELECT * FROM alertas_agendamento WHERE pessoa_id = ? AND status = 'Aguardando vaga' "
        "ORDER BY id DESC", (pid,)
    ).fetchall()
    missed_appointments = [appointment for appointment in appointments if appointment["status"] == "Faltou"]
    connection.close()
    if not person:
        return "Não encontrado", 404
    return render_template(
        "pessoa_detalhe.html", person=person, attendances=attendances, frequencies=frequencies,
        appointments=appointments, missed_appointments=missed_appointments,
        scheduling_alerts=scheduling_alerts,
    )


@app.route("/agendamentos", methods=["GET", "POST"])
def agendamentos():
    if not authenticated():
        return redirect(url_for("login"))
    connection = db()
    if request.method == "POST":
        person_id = request.form.get("pessoa_id", "")
        professional = request.form.get("profissional", "")
        appointment_date = request.form.get("data", "")
        appointment_time = request.form.get("hora", "")
        person = connection.execute("SELECT id FROM pessoas WHERE id = ?", (person_id,)).fetchone()
        if not person or professional not in PROFESSIONAL_OPTIONS or not appointment_date or not appointment_time:
            connection.close()
            flash("Preencha o assistido, o profissional, a data e a hora do retorno.")
            return redirect(url_for("agendamentos"))
        try:
            appointment_day = date.fromisoformat(appointment_date).day
        except ValueError:
            appointment_day = -1
        available = connection.execute(
            "SELECT 1 FROM disponibilidades WHERE profissional = ? AND dia_mes = ? "
            "AND hora_inicio <= ? AND hora_fim > ? LIMIT 1",
            (professional, appointment_day, appointment_time, appointment_time),
        ).fetchone()
        if not available:
            connection.close()
            flash("Esse profissional não possui disponibilidade para o dia e horário informados.")
            return redirect(url_for("agendamentos"))
        cursor = connection.execute(
            "INSERT INTO agendamentos(pessoa_id, profissional, data, hora, observacao, criado_por, criado_em) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (person_id, professional, appointment_date, appointment_time, request.form.get("observacao", ""),
             session["uid"], datetime.now().isoformat(timespec="seconds")),
        )
        appointment_id = cursor.lastrowid
        connection.commit()
        connection.close()
        audit("Agendamento criado", "agendamento", appointment_id)
        return redirect(url_for("comprovante_agendamento", appointment_id=appointment_id))
    people = connection.execute("SELECT id, nome, processo FROM pessoas ORDER BY nome").fetchall()
    availabilities = connection.execute(
        "SELECT * FROM disponibilidades WHERE dia_mes IS NOT NULL ORDER BY profissional, dia_mes, hora_inicio"
    ).fetchall()
    available_dates = {}
    current_month = date.today().replace(day=1)
    for availability in availabilities:
        try:
            available_date = current_month.replace(day=availability["dia_mes"])
        except ValueError:
            continue
        dates = available_dates.setdefault(availability["profissional"], [])
        date_value = available_date.isoformat()
        if date_value not in dates:
            dates.append(date_value)
    appointments = connection.execute(
        "SELECT a.*, p.nome, p.processo FROM agendamentos a JOIN pessoas p ON p.id = a.pessoa_id "
        "ORDER BY a.data, a.hora"
    ).fetchall()
    scheduling_alerts = connection.execute(
        "SELECT a.*, p.nome, p.processo FROM alertas_agendamento a JOIN pessoas p ON p.id = a.pessoa_id "
        "WHERE a.status = 'Aguardando vaga' ORDER BY a.profissional, a.data_preferencial, a.hora_preferencial, a.id"
    ).fetchall()
    selected_person = request.args.get("pessoa_id", "")
    connection.close()
    return render_template("agendamentos.html", people=people, appointments=appointments,
                           selected_person=selected_person, availabilities=availabilities,
                           available_dates=available_dates, current_year=current_month.year,
                           current_month=current_month.month, scheduling_alerts=scheduling_alerts)


@app.route("/agendamentos/alerta", methods=["POST"])
def criar_alerta_agendamento():
    if not authenticated():
        return redirect(url_for("login"))
    person_id = request.form.get("pessoa_id", "")
    professional = request.form.get("profissional", "")
    preferred_date = request.form.get("data_preferencial", "")
    preferred_time = request.form.get("hora_preferencial", "")
    connection = db()
    person = connection.execute("SELECT id FROM pessoas WHERE id = ?", (person_id,)).fetchone()
    if not person or professional not in PROFESSIONAL_OPTIONS:
        connection.close()
        flash("Preencha o assistido e o profissional do alerta.")
        return redirect(url_for("agendamentos"))
    if preferred_date:
        try:
            date.fromisoformat(preferred_date)
        except ValueError:
            connection.close()
            flash("Informe uma data preferencial válida.")
            return redirect(url_for("agendamentos"))
    cursor = connection.execute(
        "INSERT INTO alertas_agendamento(pessoa_id, profissional, data_preferencial, hora_preferencial, observacao, criado_por, criado_em) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (person_id, professional, preferred_date, preferred_time, request.form.get("observacao", ""),
         session["uid"], datetime.now().isoformat(timespec="seconds")),
    )
    alert_id = cursor.lastrowid
    connection.commit()
    connection.close()
    audit("Alerta de agendamento criado", "alerta_agendamento", alert_id)
    flash("Alerta de agendamento criado e incluído na fila de espera.")
    return redirect(url_for("agendamentos"))


@app.route("/agendamentos/alerta/<int:alert_id>/encerrar", methods=["POST"])
def encerrar_alerta_agendamento(alert_id):
    if not authenticated():
        return redirect(url_for("login"))
    connection = db()
    alert = connection.execute("SELECT id FROM alertas_agendamento WHERE id = ?", (alert_id,)).fetchone()
    if not alert:
        connection.close()
        return "Não encontrado", 404
    connection.execute("UPDATE alertas_agendamento SET status = 'Encerrado' WHERE id = ?", (alert_id,))
    connection.commit()
    connection.close()
    audit("Alerta de agendamento encerrado", "alerta_agendamento", alert_id)
    flash("Alerta de agendamento encerrado.")
    return redirect(url_for("agendamentos"))


@app.route("/agendamentos/disponibilidade", methods=["POST"])
def nova_disponibilidade():
    denial = admin_required()
    if denial:
        return denial
    professional = request.form.get("profissional", "")
    availability_date = request.form.get("dia_mes", "")
    start_time = request.form.get("hora_inicio", "")
    end_time = request.form.get("hora_fim", "")
    try:
        month_day = date.fromisoformat(availability_date).day
        valid_month_day = month_day in range(1, 32)
        valid_times = datetime.strptime(start_time, "%H:%M") < datetime.strptime(end_time, "%H:%M")
    except (TypeError, ValueError):
        valid_month_day = False
        valid_times = False
    if professional not in PROFESSIONAL_OPTIONS or not valid_month_day or not valid_times:
        flash("Preencha profissional, dia e um intervalo de horário válido.")
        return redirect(url_for("agendamentos"))
    connection = db()
    availability_columns = {row[1] for row in connection.execute("PRAGMA table_info(disponibilidades)")}
    if "dia_semana" in availability_columns:
        connection.execute(
            "INSERT INTO disponibilidades(profissional, dia_semana, dia_mes, hora_inicio, hora_fim, criado_por, criado_em) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (professional, month_day, month_day, start_time, end_time, session["uid"], datetime.now().isoformat(timespec="seconds")),
        )
    else:
        connection.execute(
            "INSERT INTO disponibilidades(profissional, dia_mes, hora_inicio, hora_fim, criado_por, criado_em) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (professional, month_day, start_time, end_time, session["uid"], datetime.now().isoformat(timespec="seconds")),
        )
    connection.commit()
    connection.close()
    flash("Disponibilidade cadastrada com sucesso.")
    return redirect(url_for("agendamentos"))


@app.route("/agendamentos/disponibilidade/<int:availability_id>/excluir", methods=["POST"])
def excluir_disponibilidade(availability_id):
    denial = admin_required()
    if denial:
        return denial
    connection = db()
    connection.execute("DELETE FROM disponibilidades WHERE id = ?", (availability_id,))
    connection.commit()
    connection.close()
    flash("Disponibilidade removida.")
    return redirect(url_for("agendamentos"))


@app.route("/agendamentos/<int:appointment_id>/status", methods=["POST"])
def atualizar_status_agendamento(appointment_id):
    if not authenticated():
        return redirect(url_for("login"))
    status = request.form.get("status", "")
    if status not in ("Agendado", "Compareceu", "Faltou"):
        flash("Status de comparecimento inválido.")
        return redirect(url_for("agendamentos"))
    absence_note = request.form.get("observacao_falta", "").strip() if status == "Faltou" else ""
    connection = db()
    appointment = connection.execute(
        "SELECT id FROM agendamentos WHERE id = ?", (appointment_id,)
    ).fetchone()
    if not appointment:
        connection.close()
        return "Não encontrado", 404
    connection.execute(
        "UPDATE agendamentos SET status = ?, observacao_falta = ? WHERE id = ?",
        (status, absence_note, appointment_id),
    )
    connection.commit()
    connection.close()
    audit(f"Agendamento marcado como {status.lower()}", "agendamento", appointment_id)
    flash("Comparecimento atualizado com sucesso.")
    return redirect(url_for("agendamentos"))


@app.route("/agendamentos/<int:appointment_id>/cancelar", methods=["POST"])
def cancelar_agendamento(appointment_id):
    if not authenticated():
        return redirect(url_for("login"))
    connection = db()
    appointment = connection.execute(
        "SELECT id, status FROM agendamentos WHERE id = ?", (appointment_id,)
    ).fetchone()
    if not appointment:
        connection.close()
        return "Não encontrado", 404
    if appointment["status"] != "Cancelado":
        connection.execute(
            "UPDATE agendamentos SET status = 'Cancelado' WHERE id = ?", (appointment_id,)
        )
        connection.commit()
        audit("Agendamento cancelado", "agendamento", appointment_id)
    connection.close()
    flash("Agendamento cancelado.")
    return redirect(url_for("agendamentos"))


@app.route("/agendamentos/semana")
def agenda_semana():
    if not authenticated():
        return redirect(url_for("login"))
    try:
        selected_date = date.fromisoformat(request.args.get("inicio", ""))
    except ValueError:
        selected_date = date.today()
    week_start = selected_date - timedelta(days=selected_date.weekday())
    week_end = week_start + timedelta(days=6)
    connection = db()
    appointments = connection.execute(
        "SELECT a.*, p.nome, p.processo FROM agendamentos a JOIN pessoas p ON p.id = a.pessoa_id "
        "WHERE a.data BETWEEN ? AND ? AND COALESCE(a.status, 'Agendado') <> 'Cancelado' "
        "ORDER BY a.data, a.hora",
        (week_start.isoformat(), week_end.isoformat()),
    ).fetchall()
    connection.close()
    return render_template(
        "agenda_semana.html", title="Agenda da Semana", appointments=appointments,
        week_start=week_start, week_end=week_end,
        previous_week=(week_start - timedelta(days=7)).isoformat(),
        next_week=(week_start + timedelta(days=7)).isoformat(),
    )


@app.route("/documento/<path:filename>")
def documento(filename):
    if not authenticated():
        return redirect(url_for("login"))
    requested = (UPLOADS / filename).resolve()
    if UPLOADS not in requested.parents or not requested.is_file():
        return "Não encontrado", 404
    return send_file(requested, conditional=True)


@app.route("/pessoa/<int:pid>/atendimento", methods=["GET", "POST"])
def novo_atendimento(pid):
    if not authenticated():
        return redirect(url_for("login"))
    if request.method == "POST":
        connection = db()
        if not connection.execute("SELECT 1 FROM pessoas WHERE id = ?", (pid,)).fetchone():
            connection.close()
            return "Assistido não encontrado", 404
        cursor = connection.execute(
            "INSERT INTO atendimentos(pessoa_id, %s, criado_por, criado_em) VALUES (?, %s, ?, ?)" % (
                ", ".join(ATTENDANCE_FIELDS), ", ".join("?" for _ in ATTENDANCE_FIELDS)
            ),
            (pid, *[request.form.get(field, "") for field in ATTENDANCE_FIELDS], session["uid"], datetime.now().isoformat(timespec="seconds")),
        )
        attendance_id = cursor.lastrowid
        connection.commit()
        connection.close()
        audit("Atendimento registrado", "atendimento", attendance_id)
        flash("Atendimento registrado com sucesso.")
        return redirect(url_for("pessoa", pid=pid))
    return render_template("atendimento_form.html", title="Retorno de acompanhamento", attendance_fields=ATTENDANCE_FIELDS, attendance_labels=ATTENDANCE_LABELS)


@app.route("/atendimento/<int:aid>/editar", methods=["GET", "POST"])
def editar_atendimento(aid):
    denial = admin_required()
    if denial:
        return denial
    connection = db()
    attendance = connection.execute("SELECT * FROM atendimentos WHERE id = ?", (aid,)).fetchone()
    if not attendance:
        connection.close()
        return "Não encontrado", 404
    if request.method == "POST":
        values = [request.form.get(field, "") for field in ATTENDANCE_FIELDS]
        connection.execute(
            "UPDATE atendimentos SET %s WHERE id = ?" % ", ".join(f"{field} = ?" for field in ATTENDANCE_FIELDS),
            (*values, aid),
        )
        connection.commit()
        connection.close()
        audit("Atendimento atualizado", "atendimento", aid)
        flash("Atendimento atualizado com sucesso.")
        return redirect(url_for("pessoa", pid=attendance["pessoa_id"]))
    connection.close()
    return render_template(
        "atendimento_form.html",
        title="Editar retorno de acompanhamento",
        attendance_fields=ATTENDANCE_FIELDS,
        attendance_labels=ATTENDANCE_LABELS,
        attendance=attendance,
    )


def printable(title, body):
    return render_template("printable.html", title=title, body=body)


def get_person(person_id):
    connection = db()
    person = connection.execute("SELECT * FROM pessoas WHERE id = ?", (person_id,)).fetchone()
    connection.close()
    return person


@app.route("/termo/<int:pid>")
def termo(pid):
    if not authenticated():
        return redirect(url_for("login"))
    person = get_person(pid)
    if not person:
        return "Não encontrado", 404
    identity_fields = ["nome", "nome_social", "cpf", "rg", "nome_mae", "processo", "rji", "vara", "telefone"]
    return printable("Termo de Atendimento", render_template("termo.html", person=person, identity_fields=identity_fields))


@app.route("/retorno/<int:aid>")
def retorno(aid):
    if not authenticated():
        return redirect(url_for("login"))
    connection = db()
    attendance = connection.execute(
        "SELECT a.*, p.nome, p.processo, p.medida FROM atendimentos a "
        "JOIN pessoas p ON p.id = a.pessoa_id WHERE a.id = ?", (aid,)
    ).fetchone()
    connection.close()
    if not attendance:
        return "Não encontrado", 404
    return printable("Retorno de Acompanhamento", render_template("retorno.html", attendance=attendance))


@app.route("/agendamentos/<int:appointment_id>/comprovante")
def comprovante_agendamento(appointment_id):
    if not authenticated():
        return redirect(url_for("login"))
    connection = db()
    appointment = connection.execute(
        "SELECT a.*, p.nome, p.cpf, p.processo FROM agendamentos a "
        "JOIN pessoas p ON p.id = a.pessoa_id WHERE a.id = ?", (appointment_id,)
    ).fetchone()
    connection.close()
    if not appointment:
        return "Não encontrado", 404
    return printable(
        "Comprovante de Agendamento",
        render_template("comprovante_agendamento.html", appointment=appointment),
    )


@app.route("/grupos")
def grupos():
    if not authenticated():
        return redirect(url_for("login"))
    connection = db()
    people = connection.execute(
        'SELECT * FROM pessoas WHERE grupo_responsabilizacao <> "" ORDER BY grupo_responsabilizacao, nome'
    ).fetchall()
    frequencies = connection.execute(
        "SELECT pessoa_id, encontro, status FROM frequencias "
        "WHERE pessoa_id IN (SELECT id FROM pessoas WHERE grupo_responsabilizacao <> '') "
        "ORDER BY pessoa_id, encontro"
    ).fetchall()
    connection.close()
    frequencies_by_person = {}
    for frequency in frequencies:
        frequencies_by_person.setdefault(frequency["pessoa_id"], {})[frequency["encontro"]] = frequency["status"]

    grouped = {}
    for person in people:
        person_data = dict(person)
        person_frequencies = frequencies_by_person.get(person["id"], {})
        registered = {number: status for number, status in person_frequencies.items() if status}
        if registered:
            last_number = max(registered)
            person_data["andamento_encontros"] = (
                f"{last_number}º encontro: {registered[last_number]} ({len(registered)}/10)"
            )
        else:
            person_data["andamento_encontros"] = "Nenhum encontro registrado (0/10)"
        grouped.setdefault(person["grupo_responsabilizacao"], []).append(person_data)
    return render_template("grupos.html", groups=grouped)


@app.route("/auditoria")
def auditoria():
    if not authenticated():
        return redirect(url_for("login"))
    connection = db()
    rows = connection.execute(
        "SELECT a.*, u.nome usuario FROM auditoria a LEFT JOIN users u ON u.id = a.usuario_id "
        "ORDER BY a.id DESC LIMIT 200"
    ).fetchall()
    connection.close()
    return render_template("auditoria.html", rows=rows)


@app.route("/exportar")
def exportar():
    if not authenticated():
        return redirect(url_for("login"))
    connection = db()
    rows = connection.execute("SELECT %s FROM pessoas ORDER BY id" % ",".join(FIELDS)).fetchall()
    connection.close()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([LABELS[field] for field in FIELDS])
    writer.writerows([[row[field] for field in FIELDS] for row in rows])
    return Response("﻿" + output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=ciap_atendimentos.csv"})


init_db()
bootstrap_planilha()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    cert_file = os.environ.get("CIAP_SSL_CERT")
    key_file = os.environ.get("CIAP_SSL_KEY")
    if bool(cert_file) != bool(key_file):
        raise RuntimeError("CIAP_SSL_CERT e CIAP_SSL_KEY devem ser informados juntos")
    ssl_context = (cert_file, key_file) if cert_file and key_file else None
    if os.environ.get("CIAP_HTTPS", "0") == "1" and ssl_context is None:
        raise RuntimeError("HTTPS exige CIAP_SSL_CERT e CIAP_SSL_KEY")
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=port, debug=False, ssl_context=ssl_context)
