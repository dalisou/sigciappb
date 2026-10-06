import csv
import io
import json
import os
import re
import secrets
import smtplib
import sqlite3
import hmac
import unicodedata
import uuid
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import (
    Flask,
    Response,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from supabase import Client, create_client
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

try:
    import psycopg2
    from psycopg2 import IntegrityError
    from psycopg2.extras import RealDictCursor
except ImportError:
    psycopg2 = None
    IntegrityError = RuntimeError
    RealDictCursor = None

# 1. Instância principal da aplicação
app = Flask(__name__)
IS_PRODUCTION = os.environ.get("CIAP_ENV", "").lower() == "production" or os.environ.get("FLASK_ENV", "").lower() == "production"
app.secret_key = os.environ.get("CIAP_SECRET", "ciap-dev-secret-change-me")
if IS_PRODUCTION and app.secret_key == "ciap-dev-secret-change-me":
    raise RuntimeError("CIAP_SECRET deve ser configurada em produção")

# 2. Definição dos diretórios locais e caminho do banco legado (DB)
BASE = Path(__file__).parent
DB = Path(os.environ.get("CIAP_DB_PATH", BASE / "data" / "ciap.db")).resolve()

DB.parent.mkdir(parents=True, exist_ok=True)

# 3. Configuração dinâmica do Banco de Dados (Render / SQLite Local)
db_url = os.getenv("DATABASE_URL", f"sqlite:///{DB}")

if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
supabase: Client | None = (
    create_client(SUPABASE_URL, SUPABASE_KEY)
    if SUPABASE_URL and SUPABASE_KEY
    else None
)
BUCKET_NAME = "documentos"

# 4. Configurações extras de segurança e upload
app.config.update(
    MAX_CONTENT_LENGTH=int(os.environ.get("CIAP_MAX_UPLOAD_BYTES", 25 * 1024 * 1024)),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("CIAP_HTTPS", "0") == "1",
)


@app.route("/logos/<filename>")
def logo(filename):
    logo_files = {
        "logociap.jpg": "logociap.jpg",
        "logoseap-ciap.jpg": "logoseap-ciap.jpg",
        "logociapRodape.jpg": "logociapRodape.jpg",
    }
    selected_file = logo_files.get(filename)
    if not selected_file:
        return "Não encontrado", 404
    return send_file(BASE / selected_file, mimetype="image/jpeg")

class PostgresCursor:
    def __init__(self, cursor):
        self.cursor = cursor

    def __iter__(self):
        return iter(self.cursor)

    @property
    def lastrowid(self):
        row = self.cursor.fetchone()
        return row["id"] if row else None

    def fetchone(self):
        return self.cursor.fetchone()

    def fetchall(self):
        return self.cursor.fetchall()


class PostgresConnection:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, query, parameters=()):
        query = query.replace("?", "%s")
        stripped = query.rstrip().rstrip(";")
        if stripped.lstrip().upper().startswith("INSERT ") and " RETURNING " not in stripped.upper():
            query = f"{stripped} RETURNING id"
        cursor = self.connection.cursor()
        cursor.execute(query, parameters)
        return PostgresCursor(cursor)

    def commit(self):
        self.connection.commit()

    def rollback(self):
        self.connection.rollback()

    def close(self):
        self.connection.close()


def using_postgres():
    return db_url.startswith(("postgres://", "postgresql://", "postgresql+psycopg2://"))


def postgres_url():
    return db_url.replace("postgresql+psycopg2://", "postgresql://", 1)


def supabase_storage():
    if supabase is None:
        raise RuntimeError("SUPABASE_URL e SUPABASE_KEY devem ser configuradas")
    return supabase.storage.from_(BUCKET_NAME)


def document_object_key(filename):
    filename = (filename or "").strip().replace("\\", "/")
    if not filename or filename.startswith("/") or any(
        part in {"", ".", ".."} for part in filename.split("/")
    ):
        raise ValueError("Nome de documento inválido")
    return filename


ADMIN_EMAIL = os.environ.get("CIAP_ADMIN_EMAIL", "admin@ciap.local")
ADMIN_PASSWORD = os.environ.get("CIAP_ADMIN_PASSWORD", "admin123")
if IS_PRODUCTION:
    if not os.environ.get("CIAP_ADMIN_EMAIL"):
        raise RuntimeError("CIAP_ADMIN_EMAIL deve ser configurado em produção")
    if not os.environ.get("CIAP_ADMIN_PASSWORD"):
        raise RuntimeError("CIAP_ADMIN_PASSWORD deve ser configurado em produção")
    if ADMIN_EMAIL == "admin@ciap.local" or ADMIN_PASSWORD == "admin123":
        raise RuntimeError("Credenciais administrativas padrão não podem ser usadas em produção")
    if not using_postgres():
        raise RuntimeError("DATABASE_URL PostgreSQL deve ser configurada em produção")
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError("SUPABASE_URL e SUPABASE_KEY devem ser configuradas em produção")
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
    "Outros Documentos",
]

FIELDS = [
    "nome", "nome_social", "cpf", "rg", "data_nascimento", "idade", "faixa_etaria", "nome_mae", "processo", "vara", "rji",
    "telefone", "data_atendimento", "raca", "sexo", "identidade_genero", "estado_civil", "orientacao_sexual",
    "escolaridade", "pcd", "tipo_deficiencia", "nacionalidade", "pais", "ocupacao", "profissao",
    "religiao", "diploma_legal", "artigo", "tipo_penal", "grupamento_penal", "natureza_atendimento",
    "medida", "grupo_responsabilizacao", "status", "atendimento_individual", "comparecimento",
    "termino_medida", "situacao_final", "observacoes", "prestacao_servico_comunitario",
    "local_prestacao_servico", "grupo_reflexivo", "tipo_grupo_reflexivo",
]
GROUP_STATUS_OPTIONS = ["Em andamento", "Concluiu", "Desistiu", "Eliminado - refazer grupo"]
FREQUENCY_STATUS_OPTIONS = ["", "Presente", "Faltou"]
GROUP_RESPONSIBILITY_LABELS = {
    "orientacao civica": "Orientação Cívica",
    "genero e alteridade": "Gênero e Alteridade",
}

LABELS = dict(zip(FIELDS, [
    "Nome do Assistido(a)", "Nome Social", "CPF", "RG/órgão emissor", "Data de nascimento", "Idade", "Faixa Etária", "Nome da mãe",
    "Número do Processo", "Órgão Judicial/Vara", "Nº Inscrição (ID único)", "Telefone(s) WhatsApp",
    "Data de Atendimento", "Raça/cor da pele (Declarada)", "Sexo (biológico)", "Identidade de Gênero (Declarada)", "Estado Civil",
    "Orientação Sexual (autodeclaração)", "Escolaridade", "Pessoa com Deficiência", "Tipo de Deficiência",
    "Nacionalidade", "País", "Ocupação", "Profissão", "Religião", "Diploma Legal", "Artigo/Capitulação",
    "Tipo Penal", "Grupamento Penal", "Natureza do Atendimento", "Tipo de Medida/Alternativa",
    "Grupo de Responsabilização", "Status do Atendimento", "Atendimento Individual", "Comparecimento Voluntário",
    "Data de Término da Medida", "Situação Final", "Observação/Justificativas", "Prestação de Serviço Comunitário",
    "Local da Prestação de Serviço", "Grupo Reflexivo", "Tipo de Grupo Reflexivo",
]))

SELECT_OPTIONS = {
    "raca": ["Preta", "Branca", "Parda", "Amarela", "Indígena", "Não Declarada"],
    "sexo": ["Feminino", "Masculino"],
    "identidade_genero": ["Homem Cis", "Homem Trans", "Mulher Cis", "Mulher Trans/Travesti", "Transgênero", "Pessoa Não Binária", "Outro", "Não Informou"],
    "estado_civil": ["Solteiro", "Casado", "Separado", "Divorciado", "Viúvo(a)", "União Estável"],
    "orientacao_sexual": ["Heterosexual", "Homosexual", "Bisexual", "Pansexual", "Assexual", "Demissexual", "Não Declarado"],
    "escolaridade": ["Ensino Fundamental Incompleto", "Ensino Fundamental", "Ensino Médio Incompleto", "Ensino Médio", "Ensino Superior Incompleto", "Ensino Superior", "Pós Graduado", "MBA", "Mestrado", "Doutorado", "Pós Doutorado", "Não Informado", "Não Alfabetizado"],
    "pcd": ["Sim", "Não", "Não Declarado"],
    "tipo_deficiencia": ["Não Possui", "Motora", "Visual", "Mental/Intelectual", "Auditiva", "Outra(s) Deficiência(s)", "Não Declarado"],
    "nacionalidade": ["Brasileira", "Estrangeira"],
    "ocupacao": ["Formal", "Informal", "Sem Ocupação", "Não Informou"],
    "religiao": ["Católica", "Evangélica", "Cristã", "Matriz Africana", "Espírita", "Budismo", "Judaísmo", "Islamismo", "Hinduísmo", "Fé Bahá'í", "Espiritualidade Sem Religião", "Sem Religião", "Não declarada"],
    "diploma_legal": ["Aguardando audiência", "Antiga Lei de Licitações e Contratos (Lei nº 8.666/93)", "Código de Trânsito Brasileiro (Lei 9.503/97)", "Código Penal/ECA", "Contravenções Penais (Dec. Lei 3.688/41) c/c Lei 11.340/06", "Contravenções Penais (Decreto-Lei 3.688/41)", "Crimes Tributários (Lei 8.137/90)", "Decreto-Lei nº 2.848/40 (Código Penal)", "Direção sob influência de álcool", "Estatuto da Criança e do Adolescente (Lei 8.069/90)", "Estatuto do Desarmamento (Lei 10.826/03)", "Lei 11.343/06 (Lei de Drogas)", "Lei 7.716/89 (Lei de Racismo)", "Lei 8.137/90 (Crimes Tributários)", "Lei 9.605/1998 (Lei de Crimes Ambientais)", "Lei Nº 11.340/06 (Maria da Penha)", "Lei nº 11.343/06 (Lei de Drogas)", "Lei nº 8.069/90 (ECA)", "Lei nº 9.503/97 (Código de Trânsito Brasileiro)"],
    "tipo_penal": ["Abandono material", "Adulteração de Sinal Identificador de Veículo", "Ameaça", "Apropriação Indébita", "Apropriação Indébita Tributária", "Armazenamento, transporte ou guarda de substância tóxica ou perigosa", "Associação para o tráfico", "Condução de veículo sem habilitação", "Corrupção de menores", "Dano Simples", "Descumprimento de Medida Protetiva", "Descumprimento de Medida Protetiva de Urgência", "Difamação", "Dirigir veículo sem habilitação", "Discriminação ou Preconceito de Raça, Cor, Etnia, Religião ou Procedência Nacional", "Divulgação/Transmissão de Cena de Exploração Sexual Infantojuvenil", "Embriaguez ao Volante", "Estelionato", "Falsidade de Atestado Médico", "Falsidade ideológica", "Falsificação de Documento Público", "Fraude em Licitação", "Furto", "Homicídio Culposo no Trânsito", "Importunação Sexual", "Incitação ao crime", "Incêndio", "Injúria", "Injúria Racial", "Lesão corporal", "Lesão Corporal Culposa no Trânsito", "Omissão/fraude de tributo", "Peculato", "Poluição Ambiental", "Porte de arma branca", "Posse/Porte ilegal de arma de fogo", "Receptação", "Roubo Impróprio", "Homicídio Culposo na Direção de Veículo Automotor", "Sonegação", "Sonegação fiscal", "Submeter Menor a Vexame ou Constrangimento", "Tráfico de drogas", "Uso de Documento Falso", "Velocidade Incompatível", "Venda/Exposição de Pornografia Infanto-juvenil", "Violência física/psicológica/sexual/patrimonial/moral", "Violência Sexual"],
    "grupamento_penal": ["Armas/Estatuto do Desarmamento", "Armas/Estatuto desarmamento", "Crimes contra a Família", "Crimes contra a Honra", "Crimes contra a Paz Pública", "Crimes contra a Pessoa", "Crimes contra Assistência Familiar", "Crimes Contra o Meio Ambiente", "Crimes contra o Patrimônio", "Crimes de Preconceito de Raça ou de Cor", "Crimes de Trânsito", "Dignidade Sexual", "Dos Crimes Contra o Meio Ambiente/Dos Crimes de Poluição", "Dos Crimes e das Penas", "Dos Crimes e das Penas (Licitações)", "Dos Crimes em Espécie (ECA)/Crimes Cibernéticos", "Fé pública", "Incolumidade Pública/Perigo Comum", "Lei de Drogas(ou Narcotráficos)", "Liberdade Individual", "Ordem Tributária", "Proteção da Infância/Crimes Acessórios", "Proteção à Criança e ao Adolescente", "Segurança Pública/Patrimonial", "Violência Doméstica/familiar"],
    "natureza_atendimento": ["1º Atendimento Técnico Multidisciplinar", "Acolhimento Inicial (comparecimento)", "Agendamento"],
    "medida": ["Acordo de não Persecução Penal", "Conciliação", "Medida Cautelar diversa da prisão", "Outras modalidades", "Penas restritivas de direito", "SCP", "SURSIS"],
    "grupo_responsabilizacao": ["Alteridade, Raça e Direitos Humanos", "Combate ao preconceito", "Conscientização sobre Bens Públicos e Privados", "Dignidade Sexual e Alteridade", "Direitos Humanos, Diversidade e Relações Étnico-Raciais", "Direitos Humanos, Proteção à Infância e Cidadania", "Drogas e suas transversalidades", "Gênero e Alteridade: Coexistência Feminina", "Gênero, Vínculos e Alteridade", "Gênero/Sexualidade", "Gênero/Vínculos/Alteridade", "Homens autores de violência doméstica contra mulher", "Meio Ambiente, Sustentabilidade e Ecologia Familiar", "Meio Ambiente, Sustentabilidade e Proteção Comunitária", "Orientação Cívica", "Orientação Cívica ou Temático de Segurança", "Parentalidade e Direitos Humanos", "Parentalidade e Responsabilidade Familiar", "Segurança Comunitária e Paz Pública", "Trânsito/Vida", "Ética/Cidadania"],
    "status": ["Realizado com Sucesso", "Não Realizado"],
    "atendimento_individual": ["Assistente Social", "Jurídico", "Psicólogo"],
    "comparecimento": ["Sim", "Não"],
}
ARTICLE_OPTIONS = ["2º-A", "art. 12", "Art. 12, Lei 10.826/03", "Art. 129", "Art. 129 § 9º", "Art. 129, § 13, CP", "Art. 129, § 9º c/c Art. 5º, I e II da Lei 11.340/06", "Art. 129, § 9º, CP c/c Art. 7º e 41 da Lei 11.343/06", "art. 129, §13", "art. 129, §13, CP c/c art. 5º, II, Lei nº 11.340/06", "art. 129, §9º", "Art. 129,§ 9º", "art. 139", "art. 14", "art. 140", "art. 147", "art. 150", "art. 155", "Art. 155, § 4º, II", "Art. 155, § 4º, IV c/c Art. 14, II", "Art. 155, § 4º, IV, CP c/c Art. 244-B", "art. 157", "art. 16", "art. 163", "art. 168", "Art. 168, § 1º, III", "Art. 171", "Art. 171, § 4º c/c Art. 29", "art. 171, §4º", "Art. 180", "art. 19", "art. 1º", "Art. 1º, I", "Art. 1º, I, art. 24-A", "art. 20", "art. 21", "Art. 21, LCP", "Art. 21, LCP c/c Arts. 5º e 7º da Lei 11.340/06", "art. 215", "art. 215-A", "art. 232", "art. 24-A", "art. 241", "art. 244", "art. 250", "art. 286", "art. 297", "art. 299", "art. 2º", "art. 2º-A", "art. 302", "Art. 302, § 1º, III", "art. 303", "art. 304", "art. 306", "art. 309", "art. 311", "art. 311, caput", "art. 312", "Art. 33", "art. 33", "art. 33, c/c art. 40, III", "art. 33, caput, §4º", "art. 33, §4º", "art. 35", "art. 54", "art. 56", "art. 5º", "art. 5º, III", "art. 70", "art. 7º", "art. 90", "art.168", "art.180", "art.311"]
REFLECTIVE_GROUP_OPTIONS = ["Homens autores de violência doméstica contra mulher", "Drogas e suas transversalidades", "Trânsito/Vida"]

ATTENDANCE_FIELDS = [
    "data", "inicio_fim", "profissional", "tipo_retorno", "compareceu", "busca_ativa", "emprego",
    "aderencia", "mudou_contato", "relato", "intervencao", "pendencias", "conclusao", "recomendacao",
    "data_atendimento", "horario_inicio_fim", "profissional_nome_cargo", "outro_tipo_retorno",
    "comparecimento_voluntario_opcao", "comparecimento_voluntario_observacao",
    "busca_ativa_opcao", "busca_ativa_observacao", "empregado_estudando_opcao",
    "empregado_estudando_observacao", "aderencia_medidas_opcao", "aderencia_medidas_observacao",
    "mudanca_endereco_contato_opcao", "mudanca_endereco_contato_observacao", "condicoes_judiciais",
    "relato_subjetivo", "intervencao_profissional", "encaminhamentos_pendentes", "recomendacoes",
]
ATTENDANCE_CONDITIONS = (
    ("tratamento_saude", "Tratamento de Saúde/Psicossocial (Ex: Hospital Nova Esperança)"),
    ("comparecimento_juizo", "Comparecimento Periódico em Juízo (CPF)"),
    ("psc", "Prestação de Serviços à Comunidade (PSC)"),
    ("outra", "Outra Condição"),
)
ATTENDANCE_RETURN_TYPES = (
    "1º Atendimento Técnico Multidisciplinar", "Primeiro retorno", "Retorno de rotina",
    "Retorno após ausência", "Outro",
)
ATTENDANCE_RECOMMENDATIONS = (
    "Continuar o acompanhamento de rotina",
    "Solicitar avaliação psicológica/social mais aprofundada",
    "Sugerir a modificação das medidas",
    "Informar o Juízo sobre o descumprimento",
)
ATTENDANCE_LABELS = dict(zip(ATTENDANCE_FIELDS, [
    "Data do Atendimento", "Horário de início/Fim", "Profissional responsável (Nome e Cargo)", "Tipo de Retorno",
    "Comparecimento voluntário (Sim/Não)", "Convocação/busca ativa (Sim/Não)", "Empregado ou estudando (Sim/Não)",
    "Aderência/compreensão das medidas (Sim/Não)", "Mudança de endereço/contato (Sim/Não)", "Relato subjetivo/observação",
    "Intervenção do profissional da CIAP", "Encaminhamentos pendentes/próximos passos", "Conclusão: Regular ou Irregular/Risco",
    "Recomendação ao Juízo",
]))
PROFESSIONAL_OPTIONS = [
    ("profissional_psicologo", "Profissional Psicólogo"),
    ("profissional_assistente_social", "Profissional Assistente Social"),
    ("profissional_advogado", "Profissional Advogado"),
    ("profissional_administrativo", "Profissional Administrativo"),
]
PROFESSIONAL_COUNCIL_PREFIXES = {
    "profissional_psicologo": "CRP",
    "profissional_assistente_social": "CRESS",
    "profissional_advogado": "OAB",
}
PROFESSIONAL_KEYS = {value for value, _ in PROFESSIONAL_OPTIONS}
MULTIDISCIPLINARY_PROFILES = {
    "profissional_psicologo",
    "profissional_assistente_social",
    "profissional_advogado",
}
ADMIN_PROFILE = "administrador"
MONTH_DAY_OPTIONS = [(day, str(day)) for day in range(1, 32)]
FORTALEZA_TIMEZONE = ZoneInfo("America/Fortaleza")


def normalize_group_responsibility(value):
    value = " ".join((value or "").split())
    if not value:
        return ""
    key = "".join(
        character for character in unicodedata.normalize("NFKD", value.casefold())
        if not unicodedata.combining(character)
    )
    return GROUP_RESPONSIBILITY_LABELS.get(key, value)


def group_responsibility_key(value):
    return normalize_group_responsibility(value).casefold()


def perfil_labels():
    return [(ADMIN_PROFILE, "Administrador"), *PROFESSIONAL_OPTIONS]


def perfil_valido(perfil):
    return perfil in {value for value, _ in perfil_labels()} or perfil == "profissional"


def perfil_nome(perfil):
    labels = dict(perfil_labels())
    if perfil == "profissional":
        return "Profissional"
    return labels.get(perfil, perfil.replace("_", " ").title())


def user_professional_details(form, perfil=None):
    cargo = form.get("cargo", "").strip()
    matricula = form.get("matricula", "").strip()
    conselho_regional = form.get("conselho_regional", "").strip()
    if perfil is not None and perfil not in MULTIDISCIPLINARY_PROFILES:
        conselho_regional = ""
    if len(cargo) > 120 or len(matricula) > 80 or len(conselho_regional) > 120:
        return None
    return cargo, matricula, conselho_regional


def db():
    if using_postgres():
        if psycopg2 is None:
            raise RuntimeError("psycopg2-binary é necessário para DATABASE_URL PostgreSQL")
        return PostgresConnection(psycopg2.connect(postgres_url(), cursor_factory=RealDictCursor))
    connection = sqlite3.connect(DB)
    connection.row_factory = sqlite3.Row
    return connection


def table_columns(connection, table_name):
    if using_postgres():
        rows = connection.execute(
            "SELECT column_name AS name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = ?",
            (table_name,),
        ).fetchall()
        return {row["name"] for row in rows}
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table_name})")}


def safe_schema_execute(connection, statement):
    savepoint = "ciap_schema_migration"
    try:
        connection.execute(f"SAVEPOINT {savepoint}")
        connection.execute(statement)
        connection.execute(f"RELEASE SAVEPOINT {savepoint}")
    except Exception:
        try:
            connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        except Exception:
            connection.rollback()
        app.logger.warning("Migração de schema ignorada: %s", statement, exc_info=True)


def backfill_legacy_audit_links(connection):
    connection.execute(
        "UPDATE auditoria SET assistido_id = entidade_id, "
        "assistido_nome = (SELECT nome FROM pessoas WHERE id = auditoria.entidade_id) "
        "WHERE entidade = 'pessoa' AND assistido_id IS NULL"
    )
    for entity, table in (
        ("agendamento", "agendamentos"),
        ("alerta_agendamento", "alertas_agendamento"),
        ("atendimento", "atendimentos"),
    ):
        connection.execute(
            f"UPDATE auditoria SET assistido_id = "
            f"(SELECT pessoa_id FROM {table} WHERE id = auditoria.entidade_id), "
            f"assistido_nome = (SELECT p.nome FROM pessoas p WHERE p.id = "
            f"(SELECT pessoa_id FROM {table} WHERE id = auditoria.entidade_id)) "
            f"WHERE entidade = ? AND assistido_id IS NULL "
            f"AND EXISTS (SELECT 1 FROM {table} WHERE id = auditoria.entidade_id)",
            (entity,),
        )


def init_postgres_db():
    connection = db()
    field_sql = ", ".join(f'"{field}" TEXT' for field in FIELDS)
    attendance_sql = ", ".join(f'"{field}" TEXT' for field in ATTENDANCE_FIELDS)
    statements = [
        """CREATE TABLE IF NOT EXISTS users (
            id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, nome TEXT, email TEXT UNIQUE,
            senha TEXT, cargo TEXT, perfil TEXT DEFAULT 'profissional', status TEXT DEFAULT 'pendente',
            ativo INTEGER DEFAULT 1, criado_em TEXT DEFAULT '', aprovado_em TEXT DEFAULT ''
        )""",
        f"""CREATE TABLE IF NOT EXISTS pessoas (
            id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, criado_em TEXT, criado_por BIGINT,
            documentos TEXT DEFAULT '', {field_sql}, situacao_grupo TEXT DEFAULT '', alerta_frequencia TEXT DEFAULT ''
        )""",
        f"""CREATE TABLE IF NOT EXISTS atendimentos (
            id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, pessoa_id BIGINT, {attendance_sql},
            criado_por BIGINT, criado_em TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS agendamentos (
            id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, pessoa_id BIGINT NOT NULL,
            profissional TEXT NOT NULL, data TEXT NOT NULL, hora TEXT NOT NULL, observacao TEXT DEFAULT '',
            observacao_falta TEXT DEFAULT '', status TEXT DEFAULT 'Agendado', criado_por BIGINT, criado_em TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS alertas_agendamento (
            id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, pessoa_id BIGINT NOT NULL,
            profissional TEXT NOT NULL, data_preferencial TEXT DEFAULT '', hora_preferencial TEXT DEFAULT '',
            observacao TEXT DEFAULT '', status TEXT DEFAULT 'Aguardando vaga', criado_por BIGINT, criado_em TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS disponibilidades (
            id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, profissional TEXT NOT NULL,
            dia_mes INTEGER NOT NULL, hora_inicio TEXT NOT NULL, hora_fim TEXT NOT NULL,
            criado_por BIGINT, criado_em TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS frequencias (
            id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, pessoa_id BIGINT NOT NULL,
            encontro INTEGER NOT NULL, status TEXT DEFAULT '', data TEXT DEFAULT '',
            UNIQUE(pessoa_id, encontro)
        )""",
        """CREATE TABLE IF NOT EXISTS grupos_reflexivos (
            id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, nome TEXT NOT NULL,
            status TEXT DEFAULT 'Em andamento', criado_em TEXT NOT NULL, criado_por BIGINT
        )""",
        """CREATE TABLE IF NOT EXISTS grupo_participantes (
            id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, grupo_id BIGINT NOT NULL,
            pessoa_id BIGINT NOT NULL, UNIQUE(grupo_id, pessoa_id)
        )""",
        """CREATE TABLE IF NOT EXISTS grupo_frequencias (
            id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, grupo_id BIGINT NOT NULL,
            pessoa_id BIGINT NOT NULL, encontro INTEGER NOT NULL, status TEXT DEFAULT '',
            data TEXT DEFAULT '', horario TEXT DEFAULT '', facilitadores TEXT DEFAULT '',
            UNIQUE(grupo_id, pessoa_id, encontro)
        )""",
        """CREATE TABLE IF NOT EXISTS auditoria (
            id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, usuario_id BIGINT, acao TEXT,
            entidade TEXT, entidade_id BIGINT, criado_em TEXT, usuario_nome TEXT, assistido_id BIGINT,
            assistido_nome TEXT, tipo_acao TEXT, descricao_detalhada TEXT, data_hora TEXT
        )""",
            """CREATE TABLE IF NOT EXISTS mensagens (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, remetente_id BIGINT NOT NULL,
                destinatario_id BIGINT NOT NULL, assunto TEXT DEFAULT '', mensagem TEXT NOT NULL,
                criada_em TEXT NOT NULL, atualizada_em TEXT NOT NULL, lida_em TEXT DEFAULT '',
                arquivado INTEGER DEFAULT 0
            )""",
            """CREATE TABLE IF NOT EXISTS password_reset_requests (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, email TEXT NOT NULL,
                status TEXT DEFAULT 'Pendente', criado_em TEXT NOT NULL, resolvido_em TEXT DEFAULT ''
            )""",
    ]
    for statement in statements:
        connection.execute(statement)
    message_columns = table_columns(connection, "mensagens")
    if "arquivado" not in message_columns:
        safe_schema_execute(
            connection,
            "ALTER TABLE mensagens ADD COLUMN IF NOT EXISTS arquivado INTEGER DEFAULT 0",
        )
    attendance_columns = table_columns(connection, "atendimentos")
    for field in ATTENDANCE_FIELDS:
        if field not in attendance_columns:
            safe_schema_execute(
                connection,
                f'ALTER TABLE atendimentos ADD COLUMN IF NOT EXISTS "{field}" TEXT',
            )
    user_columns = table_columns(connection, "users")
    for field in ("cargo", "matricula", "conselho_regional"):
        if field not in user_columns:
            safe_schema_execute(
                connection,
                f'ALTER TABLE users ADD COLUMN IF NOT EXISTS "{field}" TEXT',
            )
    audit_columns = table_columns(connection, "auditoria")
    for field in ("usuario_nome", "assistido_id", "assistido_nome", "tipo_acao", "descricao_detalhada", "data_hora"):
        if field not in audit_columns:
            field_type = "BIGINT" if field == "assistido_id" else "TEXT"
            safe_schema_execute(connection, f'ALTER TABLE auditoria ADD COLUMN IF NOT EXISTS "{field}" {field_type}')
    safe_schema_execute(connection, "CREATE INDEX IF NOT EXISTS idx_auditoria_data_hora ON auditoria(data_hora)")
    safe_schema_execute(connection, "CREATE INDEX IF NOT EXISTS idx_auditoria_assistido_data ON auditoria(assistido_id, data_hora)")
    safe_schema_execute(
        connection,
        "UPDATE auditoria SET tipo_acao = COALESCE(tipo_acao, acao), "
        "descricao_detalhada = COALESCE(descricao_detalhada, acao), "
        "data_hora = COALESCE(data_hora, criado_em)",
    )
    backfill_legacy_audit_links(connection)
    safe_schema_execute(
        connection,
        "ALTER TABLE pessoas ADD COLUMN IF NOT EXISTS alerta_frequencia TEXT DEFAULT ''",
    )
    person_columns = table_columns(connection, "pessoas")
    for field in (
        "idade", "faixa_etaria", "estado_civil",
        "prestacao_servico_comunitario", "local_prestacao_servico",
        "grupo_reflexivo", "tipo_grupo_reflexivo",
    ):
        if field not in person_columns:
            safe_schema_execute(
                connection,
                f'ALTER TABLE pessoas ADD COLUMN IF NOT EXISTS "{field}" TEXT DEFAULT \'\'',
            )
    group_frequency_columns = table_columns(connection, "grupo_frequencias")
    if "horario" not in group_frequency_columns:
        safe_schema_execute(
            connection,
            "ALTER TABLE grupo_frequencias ADD COLUMN IF NOT EXISTS horario TEXT DEFAULT ''",
        )
    if "facilitadores" not in group_frequency_columns:
        safe_schema_execute(
            connection,
            "ALTER TABLE grupo_frequencias ADD COLUMN IF NOT EXISTS facilitadores TEXT DEFAULT ''",
        )
    connection.execute(
        "UPDATE users SET perfil = 'administrador', status = 'aprovado' WHERE cargo = 'Administrador'"
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


def init_db():
    if using_postgres():
        init_postgres_db()
        return
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
        CREATE TABLE IF NOT EXISTS grupos_reflexivos (
            id INTEGER PRIMARY KEY, nome TEXT NOT NULL,
            status TEXT DEFAULT "Em andamento", criado_em TEXT NOT NULL, criado_por INTEGER
        );
        CREATE TABLE IF NOT EXISTS grupo_participantes (
            id INTEGER PRIMARY KEY, grupo_id INTEGER NOT NULL, pessoa_id INTEGER NOT NULL,
            UNIQUE(grupo_id, pessoa_id)
        );
        CREATE TABLE IF NOT EXISTS grupo_frequencias (
            id INTEGER PRIMARY KEY, grupo_id INTEGER NOT NULL, pessoa_id INTEGER NOT NULL,
            encontro INTEGER NOT NULL, status TEXT DEFAULT "", data TEXT DEFAULT "",
            horario TEXT DEFAULT "", facilitadores TEXT DEFAULT "",
            UNIQUE(grupo_id, pessoa_id, encontro)
        );
        CREATE TABLE IF NOT EXISTS auditoria (
            id INTEGER PRIMARY KEY, usuario_id INTEGER, acao TEXT, entidade TEXT,
            entidade_id INTEGER, criado_em TEXT, usuario_nome TEXT, assistido_id INTEGER,
            assistido_nome TEXT, tipo_acao TEXT, descricao_detalhada TEXT, data_hora TEXT
        );
            CREATE TABLE IF NOT EXISTS mensagens (
                id INTEGER PRIMARY KEY, remetente_id INTEGER NOT NULL, destinatario_id INTEGER NOT NULL,
                assunto TEXT DEFAULT '', mensagem TEXT NOT NULL, criada_em TEXT NOT NULL,
                atualizada_em TEXT NOT NULL, lida_em TEXT DEFAULT '', arquivado INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS password_reset_requests (
                id INTEGER PRIMARY KEY, email TEXT NOT NULL, status TEXT DEFAULT 'Pendente',
                criado_em TEXT NOT NULL, resolvido_em TEXT DEFAULT ''
            );
        """ % (
            ", ".join(f"{field} TEXT" for field in FIELDS),
            ", ".join(f"{field} TEXT" for field in ATTENDANCE_FIELDS),
        )
    )
    message_columns = table_columns(connection, "mensagens")
    if "arquivado" not in message_columns:
        connection.execute("ALTER TABLE mensagens ADD COLUMN arquivado INTEGER DEFAULT 0")
    attendance_columns = table_columns(connection, "atendimentos")
    for field in ATTENDANCE_FIELDS:
        if field not in attendance_columns:
            connection.execute(f'ALTER TABLE atendimentos ADD COLUMN "{field}" TEXT')
    columns = table_columns(connection, "pessoas")
    for field in (
        "idade", "faixa_etaria", "estado_civil",
        "situacao_grupo", "alerta_frequencia", "prestacao_servico_comunitario",
        "local_prestacao_servico", "grupo_reflexivo", "tipo_grupo_reflexivo",
    ):
        if field not in columns:
            connection.execute(f'ALTER TABLE pessoas ADD COLUMN {field} TEXT DEFAULT ""')
    availability_columns = table_columns(connection, "disponibilidades")
    if "dia_mes" not in availability_columns:
        connection.execute("ALTER TABLE disponibilidades ADD COLUMN dia_mes INTEGER")
    appointment_columns = table_columns(connection, "agendamentos")
    if "status" not in appointment_columns:
        connection.execute("ALTER TABLE agendamentos ADD COLUMN status TEXT DEFAULT 'Agendado'")
    if "observacao_falta" not in appointment_columns:
        connection.execute('ALTER TABLE agendamentos ADD COLUMN observacao_falta TEXT DEFAULT ""')
    group_frequency_columns = table_columns(connection, "grupo_frequencias")
    if "horario" not in group_frequency_columns:
        connection.execute('ALTER TABLE grupo_frequencias ADD COLUMN horario TEXT DEFAULT ""')
    if "facilitadores" not in group_frequency_columns:
        connection.execute('ALTER TABLE grupo_frequencias ADD COLUMN facilitadores TEXT DEFAULT ""')
    user_columns = table_columns(connection, "users")
    if "perfil" not in user_columns:
        connection.execute("ALTER TABLE users ADD COLUMN perfil TEXT DEFAULT 'profissional'")
    if "status" not in user_columns:
        connection.execute("ALTER TABLE users ADD COLUMN status TEXT DEFAULT 'aprovado'")
    if "criado_em" not in user_columns:
        connection.execute("ALTER TABLE users ADD COLUMN criado_em TEXT DEFAULT ''")
    if "aprovado_em" not in user_columns:
        connection.execute("ALTER TABLE users ADD COLUMN aprovado_em TEXT DEFAULT ''")
    for field in ("cargo", "matricula", "conselho_regional"):
        if field not in user_columns:
            connection.execute(f'ALTER TABLE users ADD COLUMN "{field}" TEXT')
    audit_columns = table_columns(connection, "auditoria")
    for field in ("usuario_nome", "assistido_id", "assistido_nome", "tipo_acao", "descricao_detalhada", "data_hora"):
        if field not in audit_columns:
            field_type = "INTEGER" if field == "assistido_id" else "TEXT"
            connection.execute(f'ALTER TABLE auditoria ADD COLUMN "{field}" {field_type}')
    connection.execute("CREATE INDEX IF NOT EXISTS idx_auditoria_data_hora ON auditoria(data_hora)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_auditoria_assistido_data ON auditoria(assistido_id, data_hora)")
    connection.execute(
        "UPDATE auditoria SET tipo_acao = COALESCE(tipo_acao, acao), "
        "descricao_detalhada = COALESCE(descricao_detalhada, acao), "
        "data_hora = COALESCE(data_hora, criado_em)"
    )
    backfill_legacy_audit_links(connection)
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


def audit_action_type(action, entity):
    normalized = (action or "").casefold()
    if entity == "pessoa":
        if "documento" in normalized:
            return "Anexo de Documento"
        if "frequência" in normalized or "frequencia" in normalized:
            return "Frequência"
        return "Cadastro" if "criado" in normalized else "Edição de Dados"
    if entity in {"agendamento", "alerta_agendamento"}:
        return "Agendamento"
    if entity == "atendimento":
        return "Atendimento Multidisciplinar"
    if entity == "grupo_reflexivo":
        return "Frequência" if "frequência" in normalized or "frequencia" in normalized else "Criar/Editar Grupo Reflexivo"
    if entity == "disponibilidade":
        return "Agendamento"
    if entity == "usuario":
        return "Gestão de Usuários"
    if entity == "mensagem":
        return "Mensagem"
    return "Outra Ação"


def format_audit_timestamp(value):
    if not value:
        return "—"
    if isinstance(value, datetime):
        timestamp = value
    else:
        try:
            timestamp = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return str(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(FORTALEZA_TIMEZONE).strftime("%d/%m/%Y %H:%M:%S")


def audit(action, entity, entity_id=None, *, assistido_id=None, assistido_nome=None,
          tipo_acao=None, descricao_detalhada=None, actor_id=None, actor_name=None):
    connection = db()
    created_at = datetime.now().isoformat(timespec="seconds")
    actor_id = session.get("uid") if actor_id is None else actor_id
    if actor_name is None:
        actor_name = session.get("nome", "")
        if not actor_name and actor_id is not None:
            actor = connection.execute("SELECT nome FROM users WHERE id = ?", (actor_id,)).fetchone()
            actor_name = actor["nome"] if actor else ""
    if tipo_acao is None:
        tipo_acao = audit_action_type(action, entity)
    if descricao_detalhada is None:
        descricao_detalhada = action

    assisted_ids = [assistido_id] if assistido_id is not None else []
    if not assisted_ids:
        if entity == "pessoa" and entity_id is not None:
            assisted_ids = [entity_id]
        elif entity in {"agendamento", "alerta_agendamento", "atendimento"} and entity_id is not None:
            table = {"agendamento": "agendamentos", "alerta_agendamento": "alertas_agendamento",
                     "atendimento": "atendimentos"}[entity]
            related = connection.execute(f"SELECT pessoa_id FROM {table} WHERE id = ?", (entity_id,)).fetchone()
            if related:
                assisted_ids = [related["pessoa_id"]]
        elif entity == "grupo_reflexivo" and entity_id is not None:
            assisted_ids = [row["pessoa_id"] for row in connection.execute(
                "SELECT pessoa_id FROM grupo_participantes WHERE grupo_id = ?", (entity_id,)
            ).fetchall()]

    if not assisted_ids:
        assisted_ids = [None]
    for related_person_id in dict.fromkeys(assisted_ids):
        related_person_name = assistido_nome
        if related_person_id is not None and related_person_name is None:
            related_person = connection.execute(
                "SELECT nome FROM pessoas WHERE id = ?", (related_person_id,)
            ).fetchone()
            related_person_name = related_person["nome"] if related_person else ""
        connection.execute(
            "INSERT INTO auditoria(usuario_id, acao, entidade, entidade_id, criado_em, usuario_nome, "
            "assistido_id, assistido_nome, tipo_acao, descricao_detalhada, data_hora) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (actor_id, action, entity, entity_id, created_at, actor_name or "", related_person_id,
             related_person_name or "", tipo_acao, descricao_detalhada, created_at),
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

def submitted_articles():
    values = request.form.getlist("artigo") or request.form.getlist("artigo_capitulacao")
    return [value.strip() for value in values if value.strip()]


def parse_selected_articles(value):
    if not value:
        return []
    if " | " in value:
        return [article.strip() for article in value.split(" | ") if article.strip()]
    if value in ARTICLE_OPTIONS:
        return [value]

    selected = []
    remaining = value.strip()
    while remaining:
        matches = [
            option for option in ARTICLE_OPTIONS
            if remaining == option or remaining.startswith(f"{option}, ")
        ]
        if not matches:
            return [value.strip()]
        article = max(matches, key=len)
        selected.append(article)
        if remaining == article:
            break
        remaining = remaining[len(article) + 2:]
    return selected


def person_form_values():
    values = [request.form.get(field, "") for field in FIELDS]

    disability_index = FIELDS.index("tipo_deficiencia")
    disability_option = request.form.get("tipo_deficiencia_opcao", "")
    if disability_option in {"Outra(s) Deficiência(s)", "Outros"}:
        values[disability_index] = request.form.get("tipo_deficiencia_outro", "").strip() or disability_option
    else:
        values[disability_index] = disability_option

    birth_date = request.form.get("data_nascimento", "")
    age = ""
    age_range = ""
    try:
        parsed_birth_date = date.fromisoformat(birth_date)
        today = date.today()
        age_value = today.year - parsed_birth_date.year
        if (today.month, today.day) < (parsed_birth_date.month, parsed_birth_date.day):
            age_value -= 1
        if age_value >= 0:
            age = str(age_value)
            for start, end in ((18, 24), (25, 29), (30, 34), (35, 39), (40, 44), (45, 49),
                               (50, 54), (55, 59), (60, 64), (65, 69), (70, 74), (75, 79), (80, 84)):
                if start <= age_value <= end:
                    age_range = f"{start} a {end}"
                    break
    except (TypeError, ValueError):
        pass
    values[FIELDS.index("idade")] = age
    values[FIELDS.index("faixa_etaria")] = age_range

    article_index = FIELDS.index("artigo")
    values[article_index] = " | ".join(submitted_articles())

    service_index = FIELDS.index("prestacao_servico_comunitario")
    location_index = FIELDS.index("local_prestacao_servico")
    if values[service_index] != "Sim":
        values[location_index] = ""

    group_index = FIELDS.index("grupo_reflexivo")
    group_type_index = FIELDS.index("tipo_grupo_reflexivo")
    if values[group_index] != "Sim":
        values[group_type_index] = ""

    return values


def save_document(uploaded, pessoa_id, categoria_id):
    filename_seguro = secure_filename(uploaded.filename or "")
    if not filename_seguro:
        raise ValueError("Nome de arquivo inválido")
    object_key = f"{pessoa_id}/{categoria_id}_{uuid.uuid4().hex}_{filename_seguro}"
    file_bytes = uploaded.read()
    supabase_storage().upload(
        path=object_key,
        file=file_bytes,
        file_options={"content-type": uploaded.content_type or "application/octet-stream"},
    )
    return object_key


def document_entries(documentos):
    entries = []
    for line in (documentos or "").splitlines():
        label, separator, filenames = line.partition(": ")
        if not separator:
            continue
        for filename in filenames.split(", "):
            filename = filename.strip()
            if filename:
                object_key = document_object_key(filename)
                entries.append({
                    "label": label.strip(),
                    "filename": object_key,
                    "display_name": object_key.rsplit("/", 1)[-1],
                })
    return entries


def document_owner_id(filename):
    connection = db()
    people = connection.execute(
        "SELECT id, documentos FROM pessoas WHERE documentos IS NOT NULL AND documentos <> ''"
    ).fetchall()
    connection.close()
    for person in people:
        if filename in {entry["filename"] for entry in document_entries(person["documentos"])}:
            return person["id"]
    return None


def remove_document_reference(documentos, filename):
    lines = []
    for line in (documentos or "").splitlines():
        label, separator, filenames = line.partition(": ")
        if not separator:
            lines.append(line)
            continue
        remaining = [
            document_object_key(item)
            for item in filenames.split(", ")
            if item.strip() and document_object_key(item) != document_object_key(filename)
        ]
        if remaining:
            lines.append(f"{label}: {', '.join(remaining)}")
    return "\n".join(lines) + ("\n" if lines else "")


def delete_document(filename):
    object_key = document_object_key(filename)
    supabase_storage().remove([object_key])


@app.before_request
def protect_state_changes():
    if request.method in {"POST", "DELETE"}:
        submitted = request.form.get("_csrf_token", "") or request.headers.get("X-CSRF-Token", "")
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
        "labels": LABELS, "fields": FIELDS, "docs": DOCS, "documents": [],
        "select_options": SELECT_OPTIONS, "article_options": ARTICLE_OPTIONS,
        "reflective_group_options": REFLECTIVE_GROUP_OPTIONS,
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
        professional_details = user_professional_details(request.form)
        if not name or not EMAIL_PATTERN.fullmatch(email):
            flash("Informe um nome e um e-mail válido.")
            return render_template("cadastro.html", title="Solicitar acesso")
        if professional_details is None:
            flash("Cargo, matrícula ou conselho regional excede o tamanho permitido.")
            return render_template("cadastro.html", title="Solicitar acesso")
        if not PASSWORD_PATTERN.fullmatch(password):
            flash("A senha deve ter no mínimo 6 caracteres, usando apenas letras e números.")
            return render_template("cadastro.html", title="Solicitar acesso")
        if password != confirmation:
            flash("A confirmação da senha não confere.")
            return render_template("cadastro.html", title="Solicitar acesso")
        connection = db()
        try:
            cursor = connection.execute(
                "INSERT INTO users(nome, email, senha, cargo, matricula, conselho_regional, perfil, status, ativo, criado_em) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (name, email, generate_password_hash(password), *professional_details, "profissional",
                 "pendente", 0, datetime.now().isoformat(timespec="seconds")),
            )
            user_id = cursor.lastrowid
            connection.commit()
        except (sqlite3.IntegrityError, IntegrityError):
            if using_postgres():
                connection.rollback()
            connection.close()
            flash("Este e-mail já possui cadastro ou solicitação.")
            return render_template("cadastro.html", title="Solicitar acesso")
        connection.close()
        audit(
            "Solicitação de acesso criada", "usuario", user_id,
            actor_id=user_id, actor_name=name, tipo_acao="Gestão de Usuários",
            descricao_detalhada="Solicitação de acesso enviada para aprovação administrativa.",
        )
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


@app.route("/recuperacao-senha", methods=["POST"])
def solicitar_recuperacao_senha():
    email = request.form.get("email_recuperacao", "").strip().lower()
    if not EMAIL_PATTERN.fullmatch(email):
        flash("Informe um e-mail válido para solicitar a recuperação.")
        return redirect(url_for("login"))
    connection = db()
    pending = connection.execute(
        "SELECT 1 FROM password_reset_requests WHERE lower(email) = lower(?) AND status = 'Pendente'",
        (email,),
    ).fetchone()
    if not pending:
        connection.execute(
            "INSERT INTO password_reset_requests(email, status, criado_em) VALUES (?, 'Pendente', ?)",
            (email, datetime.now().isoformat(timespec="seconds")),
        )
        connection.commit()
        audit("Solicitação de recuperação de senha criada", "recuperacao_senha",
              tipo_acao="Gestão de Usuários", descricao_detalhada="Solicitação de recuperação de senha recebida.",
              actor_name="Solicitante não autenticado")
    connection.close()
    flash("Solicitação recebida. A equipe responsável dará continuidade ao atendimento.")
    return redirect(url_for("login"))


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
    password_requests = connection.execute(
        "SELECT pr.*, u.id AS user_id, u.nome AS user_nome FROM password_reset_requests pr "
        "LEFT JOIN users u ON lower(u.email) = lower(pr.email) "
        "WHERE pr.status = 'Pendente' ORDER BY pr.criado_em, pr.id"
    ).fetchall()
    connection.close()
    return render_template(
        "solicitacoes.html", title="Perfis", requests=requests, users=users,
        password_requests=password_requests,
    )


@app.route("/perfis/recuperacao-senha/<int:request_id>/resolver", methods=["POST"])
def resolver_recuperacao_senha(request_id):
    denial = admin_required()
    if denial:
        return denial
    connection = db()
    reset_request = connection.execute(
        "SELECT id FROM password_reset_requests WHERE id = ? AND status = 'Pendente'",
        (request_id,),
    ).fetchone()
    if not reset_request:
        connection.close()
        return "Solicitação não encontrada", 404
    connection.execute(
        "UPDATE password_reset_requests SET status = 'Atendida', resolvido_em = ? WHERE id = ?",
        (datetime.now().isoformat(timespec="seconds"), request_id),
    )
    connection.commit()
    connection.close()
    audit("Solicitação de recuperação de senha atendida", "recuperacao_senha", request_id)
    flash("Solicitação marcada como atendida.")
    return redirect(url_for("perfis"))


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
        professional_details = user_professional_details(request.form, perfil)
        if not name or not EMAIL_PATTERN.fullmatch(email):
            flash("Informe um nome e um e-mail válido.")
        elif not perfil_valido(perfil) or status not in ("pendente", "aprovado", "rejeitado"):
            flash("Perfil ou status inválido.")
        elif professional_details is None:
            flash("Cargo, matrícula ou conselho regional excede o tamanho permitido.")
        elif password and not PASSWORD_PATTERN.fullmatch(password):
            flash("A senha deve ter no mínimo 6 caracteres, usando apenas letras e números.")
        else:
            try:
                fields = ["nome = ?", "email = ?", "perfil = ?", "cargo = ?", "matricula = ?",
                          "conselho_regional = ?", "status = ?", "ativo = ?"]
                cargo = professional_details[0] or perfil_nome(perfil)
                values = [name, email, perfil, cargo, *professional_details[1:],
                          status, 1 if status == "aprovado" else 0]
                if password:
                    fields.append("senha = ?")
                    values.append(generate_password_hash(password))
                values.append(user_id)
                connection.execute("UPDATE users SET %s WHERE id = ?" % ", ".join(fields), values)
                connection.commit()
            except (sqlite3.IntegrityError, IntegrityError):
                if using_postgres():
                    connection.rollback()
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
    perfil = request.form.get("perfil", ADMIN_PROFILE)
    if not perfil_valido(perfil) or perfil == "profissional":
        flash("Perfil inválido.")
        return redirect(url_for("perfis"))
    connection = db()
    user = connection.execute("SELECT * FROM users WHERE id = ? AND status = 'pendente'", (user_id,)).fetchone()
    if not user:
        connection.close()
        return "Solicitação não encontrada", 404
    now = datetime.now().isoformat(timespec="seconds")
    cargo = user["cargo"] or perfil_nome(perfil)
    connection.execute(
        "UPDATE users SET perfil = ?, cargo = ?, conselho_regional = ?, status = 'aprovado', "
        "ativo = 1, aprovado_em = ? WHERE id = ?",
        (perfil, cargo, user["conselho_regional"] if perfil in MULTIDISCIPLINARY_PROFILES else "", now, user_id),
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
    group_absences = connection.execute(
        "SELECT gf.grupo_id, gf.pessoa_id, gf.encontro, gf.data, gf.horario, gf.facilitadores, "
        "g.nome AS grupo_nome, p.nome AS pessoa_nome, p.cpf, p.processo "
        "FROM grupo_frequencias gf "
        "JOIN grupos_reflexivos g ON g.id = gf.grupo_id "
        "JOIN pessoas p ON p.id = gf.pessoa_id "
        "WHERE gf.status = 'Faltou' ORDER BY gf.data DESC, g.nome, p.nome, gf.encontro"
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
        group_absences=group_absences,
        filters={"inicio": start_date, "fim": end_date, "profissional": professional, "justificativa": note_filter},
    )


@app.route("/relatorios/senappen")
def relatorio_senappen():
    if not authenticated():
        return redirect(url_for("login"))

    today = date.today()
    default_semester = 1 if today.month <= 6 else 2
    try:
        year = int(request.args.get("ano", today.year))
        semester = int(request.args.get("semestre", default_semester))
    except (TypeError, ValueError):
        year, semester = today.year, default_semester
    if not 1900 <= year <= 2100 or semester not in (1, 2):
        year, semester = today.year, default_semester

    first_month = 1 if semester == 1 else 7
    start = date(year, first_month, 1)
    month_count = 6
    month_labels = ["Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
                    "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro"]
    months = [
        {"key": f"{year}-{month:02d}", "label": month_labels[month - 1], "total": 0}
        for month in range(first_month, first_month + month_count)
    ]
    start_text = start.isoformat()
    end_text = date(year, first_month + month_count, 1).isoformat() if first_month == 1 else date(year + 1, 1, 1).isoformat()

    connection = db()
    people = connection.execute(
        "SELECT data_atendimento, data_nascimento, faixa_etaria, idade, medida, "
        "identidade_genero, raca, pcd, tipo_deficiencia, escolaridade, ocupacao, nacionalidade, pais "
        "FROM pessoas WHERE data_atendimento >= ? AND data_atendimento < ?",
        (start_text, end_text),
    ).fetchall()
    finalized = connection.execute(
        "SELECT medida, termino_medida FROM pessoas "
        "WHERE termino_medida >= ? AND termino_medida < ?",
        (start_text, end_text),
    ).fetchall()
    connection.close()

    def key(value):
        normalized = unicodedata.normalize("NFKD", str(value or "").strip().casefold())
        return " ".join("".join(char for char in normalized if not unicodedata.combining(char)).split())

    def categories(labels):
        return {label: 0 for label in labels}

    for person in people:
        entry_date = (person["data_atendimento"] or "")[:10]
        month_key = entry_date[:7]
        for item in months:
            if item["key"] == month_key:
                item["total"] += 1

    modality_labels = [
        "Acordo de não persecução penal", "Conciliação", "Justiça restaurativa", "Mediação",
        "Medida cautelar diversa da prisão", "Medidas protetivas de urgência", "Penas restritivas de direito",
        "Suspensão condicional da pena", "Suspensão condicional do processo", "Transação penal", "Outras modalidades",
    ]
    modality_aliases = {
        "acordo de nao persecução penal": "Acordo de não persecução penal",
        "acordo de nao persecucao penal": "Acordo de não persecução penal",
        "sursis": "Suspensão condicional da pena",
        "suspensao condicional da pena": "Suspensão condicional da pena",
        "scp": "Suspensão condicional do processo",
        "suspensao condicional do processo": "Suspensão condicional do processo",
    }
    normalized_modalities = {key(label): label for label in modality_labels}

    def modality(value):
        value_key = key(value)
        return modality_aliases.get(value_key, normalized_modalities.get(value_key, "Outras modalidades"))

    entries_by_modality = categories(modality_labels)
    finalizations_by_month = {item["key"]: 0 for item in months}
    finalizations_by_modality = categories(modality_labels)
    for person in people:
        entries_by_modality[modality(person["medida"])] += 1
    for person in finalized:
        completion_date = (person["termino_medida"] or "")[:10]
        month_key = completion_date[:7]
        if month_key in finalizations_by_month:
            finalizations_by_month[month_key] += 1
        finalizations_by_modality[modality(person["medida"])] += 1
    for item in months:
        item["finalizations"] = finalizations_by_month[item["key"]]

    def count_field(field, labels, aliases=None, fallback=None):
        counts = categories(labels)
        normalized = {key(label): label for label in labels}
        normalized.update(aliases or {})
        for person in people:
            value_key = key(person[field])
            label = normalized.get(value_key, fallback if value_key else labels[-1])
            counts[label] += 1
        return counts

    gender_labels = ["Mulher Cisgênero", "Homem Cisgênero", "Mulher Trans/Travesti", "Homem Trans", "Pessoa não binária", "Outro", "Não informou"]
    gender_aliases = {
        "mulher cis": "Mulher Cisgênero", "mulher cisgenero": "Mulher Cisgênero",
        "homem cis": "Homem Cisgênero", "homem cisgenero": "Homem Cisgênero",
        "pessoa nao binaria": "Pessoa não binária", "nao informou": "Não informou",
        "nao informado": "Não informou", "transgenero": "Outro",
    }
    gender = count_field("identidade_genero", gender_labels, gender_aliases, "Outro")

    age_labels = ["18 a 24 anos", "25 a 29 anos", "30 a 34 anos", "35 a 59 anos", "60 a 74 anos", "75 anos ou mais", "Não informou"]
    ages = categories(age_labels)
    for person in people:
        age_value = key(person["faixa_etaria"])
        age = None
        if not age_value:
            try:
                birth = date.fromisoformat((person["data_nascimento"] or "")[:10])
                entry_date = (person["data_atendimento"] or "")[:10]
                reference_date = date.fromisoformat(entry_date) if entry_date else start
                age = reference_date.year - birth.year - ((reference_date.month, reference_date.day) < (birth.month, birth.day))
            except ValueError:
                try:
                    age = int(person["idade"])
                except (TypeError, ValueError):
                    pass
        if age is not None:
            age_value = "18 a 24 anos" if 18 <= age <= 24 else "25 a 29 anos" if 25 <= age <= 29 else "30 a 34 anos" if 30 <= age <= 34 else "35 a 59 anos" if 35 <= age <= 59 else "60 a 74 anos" if 60 <= age <= 74 else "75 anos ou mais" if age >= 75 else "Não informou"
        age_aliases = {"18 a 24": "18 a 24 anos", "25 a 29": "25 a 29 anos", "30 a 34": "30 a 34 anos", "35 a 59": "35 a 59 anos", "60 a 74": "60 a 74 anos", "75 ou mais": "75 anos ou mais", "nao informado": "Não informou"}
        normalized_ages = {key(label): label for label in age_labels}
        normalized_ages.update(age_aliases)
        ages[normalized_ages.get(age_value, "Não informou")] += 1

    race_labels = ["Preta", "Branca", "Parda", "Indígena", "Amarela", "Outro", "Não informou"]
    race = count_field("raca", race_labels, {"nao declarada": "Não informou", "nao informado": "Não informou"}, "Outro")
    disability_labels = ["Deficiência Motora", "Deficiência Visual", "Deficiência Mental/Intelectual", "Deficiência Auditiva", "Outra", "Não informou"]
    disability_aliases = {
        "motora": "Deficiência Motora", "visual": "Deficiência Visual",
        "mental/intelectual": "Deficiência Mental/Intelectual", "auditiva": "Deficiência Auditiva",
        "outra(s) deficiencia(s)": "Outra", "outra deficiencia": "Outra",
        "nao declarado": "Não informou", "nao declarada": "Não informou",
    }
    disability = categories(disability_labels)
    for person in people:
        if key(person["pcd"]) in {"nao", "nao possui"}:
            continue
        value_key = key(person["tipo_deficiencia"])
        if key(person["pcd"]) not in {"sim", "yes"}:
            disability["Não informou"] += 1
        else:
            normalized_disability = {key(label): label for label in disability_labels}
            normalized_disability.update(disability_aliases)
            disability[normalized_disability.get(value_key, "Não informou")] += 1

    education_labels = ["Não Alfabetizado", "Ensino Fundamental Incompleto", "Ensino Fundamental", "Ensino Médio Incompleto", "Ensino Médio", "Ensino Superior", "Pós-Graduado", "Não informou"]
    education_aliases = {
        "nao informado": "Não informou", "nao alfabetizada": "Não Alfabetizado",
        "ensino superior incompleto": "Ensino Superior", "pos graduado": "Pós-Graduado",
        "mba": "Pós-Graduado", "mestrado": "Pós-Graduado", "doutorado": "Pós-Graduado",
        "pos doutorado": "Pós-Graduado",
    }
    education = count_field("escolaridade", education_labels, education_aliases, "Não informou")
    occupation_labels = ["Ocupação Formal", "Ocupação Informal", "Sem Ocupação", "Não informou"]
    occupation = count_field("ocupacao", occupation_labels, {
        "formal": "Ocupação Formal", "informal": "Ocupação Informal",
        "sem ocupacao": "Sem Ocupação", "nao informou": "Não informou", "nao informado": "Não informou",
    }, "Não informou")

    nationality = {"Brasileiros": 0, "Estrangeiros": 0, "Não informado": 0}
    countries = {}
    for person in people:
        country = (person["pais"] or "").strip()
        nationality_key = key(person["nacionalidade"])
        country_key = key(country)
        if nationality_key in {"brasileira", "brasileiro", "brasileira nata", "brasileiro nato"} or country_key in {"brasil", "brasileira", "brasileiro"}:
            nationality["Brasileiros"] += 1
        elif nationality_key in {"estrangeira", "estrangeiro"} or country:
            nationality["Estrangeiros"] += 1
            country_name = country or "Não informado"
            countries[country_name] = countries.get(country_name, 0) + 1
        else:
            nationality["Não informado"] += 1

    return render_template(
        "relatorios/senappen.html", title="Relatório Semestral SENAPPEN",
        year=year, semester=semester, months=months,
        entries_by_modality=entries_by_modality, finalizations_by_modality=finalizations_by_modality,
        gender=gender, ages=ages, race=race, disability=disability,
        education=education, occupation=occupation, nationality=nationality,
        countries=sorted(countries.items(), key=lambda item: (-item[1], item[0].casefold())),
        total_entries=len(people), total_finalizations=len(finalized),
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
    message_rows = connection.execute(
        "SELECT m.*, sender.nome remetente, recipient.nome destinatario "
        "FROM mensagens m JOIN users sender ON sender.id = m.remetente_id "
        "JOIN users recipient ON recipient.id = m.destinatario_id "
        "WHERE (m.remetente_id = ? OR m.destinatario_id = ?) "
        "AND COALESCE(m.arquivado, 0) = 0 ORDER BY m.id DESC LIMIT 30",
        (session["uid"], session["uid"]),
    ).fetchall()
    message_users = connection.execute(
        "SELECT id, nome, cargo, perfil FROM users "
        "WHERE id <> ? AND ativo = 1 AND status = 'aprovado' ORDER BY nome",
        (session["uid"],),
    ).fetchall()
    unread_messages = sum(
        1 for message in message_rows
        if message["destinatario_id"] == session["uid"] and not message["lida_em"]
    )
    scheduling_alerts = connection.execute(
        "SELECT a.*, p.nome, p.processo FROM alertas_agendamento a "
        "JOIN pessoas p ON p.id = a.pessoa_id "
        "WHERE a.status = 'Aguardando vaga' "
        "ORDER BY a.profissional, a.data_preferencial, a.hora_preferencial, a.id"
    ).fetchall()
    connection.close()
    return render_template(
        "dashboard.html", total=total, attendances=attendances, groups_count=groups_count,
        people=people, chart_data=json.dumps(chart_data, ensure_ascii=False), chart_fields=chart_fields,
        messages=message_rows, message_users=message_users, unread_messages=unread_messages,
        scheduling_alerts=scheduling_alerts,
    )


@app.route("/mensagens/historico")
def historico_mensagens():
    if not authenticated():
        return redirect(url_for("login"))
    connection = db()
    messages = connection.execute(
        "SELECT m.*, sender.nome remetente, recipient.nome destinatario "
        "FROM mensagens m JOIN users sender ON sender.id = m.remetente_id "
        "JOIN users recipient ON recipient.id = m.destinatario_id "
        "WHERE m.remetente_id = ? OR m.destinatario_id = ? ORDER BY m.id DESC",
        (session["uid"], session["uid"]),
    ).fetchall()
    connection.close()
    return render_template("mensagens_historico.html", messages=messages)


@app.route("/mensagens", methods=["POST"])
def criar_mensagem():
    if not authenticated():
        return redirect(url_for("login"))
    recipient_id = request.form.get("destinatario_id", "")
    subject = request.form.get("assunto", "").strip()
    body = request.form.get("mensagem", "").strip()
    if not body or len(body) > 5000:
        flash("Escreva uma mensagem com até 5.000 caracteres.")
        return redirect(url_for("dashboard"))
    connection = db()
    recipient = connection.execute(
        "SELECT id FROM users WHERE id = ? AND id <> ? AND ativo = 1 AND status = 'aprovado'",
        (recipient_id, session["uid"]),
    ).fetchone()
    if not recipient:
        connection.close()
        flash("Selecione um destinatário válido.")
        return redirect(url_for("dashboard"))
    now = datetime.now().isoformat(timespec="seconds")
    cursor = connection.execute(
        "INSERT INTO mensagens(remetente_id, destinatario_id, assunto, mensagem, criada_em, atualizada_em) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (session["uid"], recipient_id, subject[:160], body, now, now),
    )
    message_id = cursor.lastrowid
    connection.commit()
    connection.close()
    audit("Mensagem enviada", "mensagem", message_id)
    flash("Mensagem enviada.")
    return redirect(url_for("dashboard"))


@app.route("/mensagens/<int:message_id>/excluir", methods=["POST", "DELETE"])
def excluir_mensagem(message_id):
    if not authenticated():
        return ("Não autenticado", 401) if request.method == "DELETE" else redirect(url_for("login"))
    connection = db()
    message = connection.execute(
        "SELECT id FROM mensagens WHERE id = ? AND (remetente_id = ? OR destinatario_id = ?)",
        (message_id, session["uid"], session["uid"]),
    ).fetchone()
    if not message:
        connection.close()
        return "Mensagem não encontrada", 404
    connection.execute("DELETE FROM mensagens WHERE id = ?", (message_id,))
    connection.commit()
    connection.close()
    audit("Mensagem excluída", "mensagem", message_id)
    if request.method == "DELETE":
        return "", 204
    flash("Mensagem excluída.")
    return redirect(url_for("dashboard"))


@app.route("/mensagens/<int:message_id>/arquivar", methods=["POST"])
def arquivar_mensagem(message_id):
    if not authenticated():
        return redirect(url_for("login"))
    connection = db()
    message = connection.execute(
        "SELECT id FROM mensagens WHERE id = ? AND (remetente_id = ? OR destinatario_id = ?)",
        (message_id, session["uid"], session["uid"]),
    ).fetchone()
    if not message:
        connection.close()
        return "Mensagem não encontrada", 404
    connection.execute("UPDATE mensagens SET arquivado = 1 WHERE id = ?", (message_id,))
    connection.commit()
    connection.close()
    audit("Mensagem arquivada", "mensagem", message_id)
    flash("Mensagem arquivada.")
    return redirect(url_for("dashboard"))


@app.route("/mensagens/<int:message_id>/desarquivar", methods=["POST"])
def desarquivar_mensagem(message_id):
    if not authenticated():
        return redirect(url_for("login"))
    connection = db()
    message = connection.execute(
        "SELECT id FROM mensagens WHERE id = ? AND (remetente_id = ? OR destinatario_id = ?)",
        (message_id, session["uid"], session["uid"]),
    ).fetchone()
    if not message:
        connection.close()
        return "Mensagem não encontrada", 404
    connection.execute("UPDATE mensagens SET arquivado = 0 WHERE id = ?", (message_id,))
    connection.commit()
    connection.close()
    audit("Mensagem desarquivada", "mensagem", message_id)
    flash("Mensagem restaurada aos comunicados ativos.")
    return redirect(url_for("historico_mensagens"))


@app.route("/mensagens/<int:message_id>/editar", methods=["GET", "POST"])
def editar_mensagem(message_id):
    if not authenticated():
        return redirect(url_for("login"))
    connection = db()
    message = connection.execute(
        "SELECT m.*, recipient.nome destinatario FROM mensagens m "
        "JOIN users recipient ON recipient.id = m.destinatario_id "
        "WHERE m.id = ? AND m.remetente_id = ?",
        (message_id, session["uid"]),
    ).fetchone()
    if not message:
        connection.close()
        return "Mensagem não encontrada", 404
    if request.method == "POST":
        subject = request.form.get("assunto", "").strip()
        body = request.form.get("mensagem", "").strip()
        if not body or len(body) > 5000:
            flash("Escreva uma mensagem com até 5.000 caracteres.")
        else:
            now = datetime.now().isoformat(timespec="seconds")
            connection.execute(
                "UPDATE mensagens SET assunto = ?, mensagem = ?, atualizada_em = ? WHERE id = ?",
                (subject[:160], body, now, message_id),
            )
            connection.commit()
            connection.close()
            audit("Mensagem editada", "mensagem", message_id)
            flash("Mensagem atualizada.")
            return redirect(url_for("dashboard"))
    connection.close()
    return render_template("mensagem_form.html", title="Editar mensagem", message=message)


@app.route("/mensagens/<int:message_id>/ler", methods=["POST"])
def marcar_mensagem_lida(message_id):
    if not authenticated():
        return redirect(url_for("login"))
    connection = db()
    update_cursor = connection.execute(
        "UPDATE mensagens SET lida_em = ? WHERE id = ? AND destinatario_id = ? AND lida_em = ''",
        (datetime.now().isoformat(timespec="seconds"), message_id, session["uid"]),
    )
    changed = update_cursor.cursor.rowcount if using_postgres() else connection.execute(
        "SELECT changes() AS total"
    ).fetchone()["total"]
    connection.commit()
    connection.close()
    if changed:
        audit("Mensagem marcada como lida", "mensagem", message_id)
    return redirect(url_for("dashboard"))


@app.route("/pessoas")
def pessoas():
    if not authenticated():
        return redirect(url_for("login"))
    query = request.args.get("q", "").strip()
    group = request.args.get("grupo", "")
    connection = db()
    tokens = query.split()
    if using_postgres() and tokens:
        # O nome usa unaccent tokenizado; CPF/processo são comparados sem máscara.
        conditions = []
        parameters = []
        for token in tokens:
            term = f"%{token}%"
            conditions.append("public.unaccent(COALESCE(nome, '')) ILIKE public.unaccent(CAST(%s AS text))")
            parameters.append(term)
        conditions = ["(" + " AND ".join(conditions) + ")"]
        clean_query = re.sub(r"[./-]", "", query)
        if clean_query:
            conditions.append(
                "(regexp_replace(COALESCE(cpf, ''), '[./-]', '', 'g') ILIKE "
                + "%s"
                + " OR regexp_replace(COALESCE(processo, ''), '[./-]', '', 'g') ILIKE "
                + "%s"
                + ")"
            )
            parameters.extend((f"%{clean_query}%", f"%{clean_query}%"))
        query_sql = "SELECT * FROM pessoas WHERE (" + " OR ".join(conditions) + ") ORDER BY nome"
        people = connection.execute(query_sql, tuple(parameters)).fetchall()
    else:
        people = connection.execute("SELECT * FROM pessoas ORDER BY nome").fetchall()
        if tokens:
            # O SQLite não possui unaccent nativo; NFKD remove acentos para a busca local.
            def search_key(value):
                normalized = unicodedata.normalize("NFKD", (value or "").casefold())
                return "".join(character for character in normalized if not unicodedata.combining(character))

            normalized_tokens = [search_key(token) for token in tokens]
            clean_query = re.sub(r"[./-]", "", search_key(query))
            people = [
                person for person in people
                if all(token in search_key(person["nome"]) for token in normalized_tokens)
                or (clean_query and clean_query in re.sub(r"[./-]", "", search_key(person["cpf"])))
                or (clean_query and clean_query in re.sub(r"[./-]", "", search_key(person["processo"])))
            ]
    people = [dict(person, grupo_responsabilizacao=normalize_group_responsibility(person["grupo_responsabilizacao"])) for person in people]
    if group:
        people = [person for person in people if group_responsibility_key(person["grupo_responsabilizacao"]) == group]
    group_names = sorted({person["grupo_responsabilizacao"] for person in people if person["grupo_responsabilizacao"]}, key=str.casefold)
    groups = [(group_responsibility_key(name), name) for name in group_names]
    connection.close()
    return render_template("pessoas.html", people=people, groups=groups, query=query, group=group)


@app.route("/pessoa/nova", methods=["GET", "POST"])
def nova():
    denial = admin_required()
    if denial:
        return denial
    if request.method == "POST":
        connection = db()
        values = person_form_values()
        duplicate = find_duplicate_person(connection, values)
        if duplicate:
            connection.close()
            flash(f"Cadastro duplicado: o identificador {duplicate} já pertence a outro assistido.")
            return render_template(
                "pessoa_form.html", title="Novo cadastro",
                artigos_selecionados=submitted_articles(),
                lista_opcoes_artigos=ARTICLE_OPTIONS,
            )
        cursor = connection.execute(
            "INSERT INTO pessoas(criado_em, criado_por, %s) VALUES (?, ?, %s)" % (
                ", ".join(FIELDS), ", ".join("?" for _ in FIELDS)
            ),
            (datetime.now().isoformat(timespec="seconds"), session["uid"], *values),
        )
        person_id = cursor.lastrowid
        uploaded_count = 0
        for index, document_label in enumerate(DOCS):
            names = []
            for uploaded in request.files.getlist(f"doc_{index}"):
                if uploaded and uploaded.filename:
                    object_key = save_document(uploaded, person_id, index)
                    names.append(object_key)
                    uploaded_count += 1
            if names:
                connection.execute(
                    "UPDATE pessoas SET documentos = COALESCE(documentos, '') || ? WHERE id = ?",
                    (f"{document_label}: {', '.join(names)}\n", person_id),
                )
        connection.commit()
        connection.close()
        audit("Cadastro criado", "pessoa", person_id)
        if uploaded_count:
            audit(f"{uploaded_count} documento(s) anexado(s) no cadastro", "pessoa", person_id,
                  tipo_acao="Anexo de Documento")
        flash("Cadastro salvo com sucesso.")
        return redirect(url_for("pessoa", pid=person_id))
    return render_template(
        "pessoa_form.html", title="Novo cadastro", artigos_selecionados=[],
        lista_opcoes_artigos=ARTICLE_OPTIONS,
    )
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
        values = person_form_values()
        changed_fields = [
            LABELS.get(field, field)
            for field, value in zip(FIELDS, values)
            if (person[field] or "") != (value or "")
        ]
        duplicate = find_duplicate_person(connection, values, exclude_id=pid)
        if duplicate:
            connection.close()
            flash(f"Cadastro duplicado: o identificador {duplicate} já pertence a outro assistido.")
            return render_template(
                "pessoa_form.html", title="Editar cadastro", person=person,
                documents=document_entries(person["documentos"]),
                artigos_selecionados=submitted_articles(),
                lista_opcoes_artigos=ARTICLE_OPTIONS,
            )
        connection.execute(
            "UPDATE pessoas SET %s WHERE id = ?" % ", ".join(f"{field} = ?" for field in FIELDS),
            (*values, pid),
        )
        uploaded_count = 0
        for index, document_label in enumerate(DOCS):
            names = []
            for uploaded in request.files.getlist(f"doc_{index}"):
                if uploaded and uploaded.filename:
                    object_key = save_document(uploaded, pid, index)
                    names.append(object_key)
                    uploaded_count += 1
            if names:
                connection.execute(
                    "UPDATE pessoas SET documentos = COALESCE(documentos, '') || ? WHERE id = ?",
                    (f"{document_label}: {', '.join(names)}\n", pid),
                )
        connection.commit()
        connection.close()
        audit(
            "Cadastro atualizado", "pessoa", pid,
            descricao_detalhada=(
                f"Campos alterados: {', '.join(changed_fields)}" if changed_fields
                else "Cadastro salvo sem alterações nos campos cadastrais."
            ),
        )
        if uploaded_count:
            audit(f"{uploaded_count} documento(s) anexado(s) ao cadastro", "pessoa", pid,
                  tipo_acao="Anexo de Documento")
        flash("Cadastro atualizado com sucesso.")
        return redirect(url_for("pessoa", pid=pid))
    connection.close()
    return render_template(
        "pessoa_form.html", title="Editar cadastro", person=person,
        documents=document_entries(person["documentos"]),
        artigos_selecionados=parse_selected_articles(person["artigo"]),
        lista_opcoes_artigos=ARTICLE_OPTIONS,
    )


@app.route("/pessoa/<int:pid>/documento/excluir", methods=["POST"])
def excluir_documento(pid):
    wants_json = request.accept_mimetypes.best == "application/json"
    if not authenticated():
        if wants_json:
            return jsonify(error="Sessão expirada. Entre novamente."), 401
        return redirect(url_for("login"))
    if not is_admin():
        if wants_json:
            return jsonify(error="Acesso permitido apenas para administradores."), 403
        return "Acesso permitido apenas para administradores.", 403
    filename = request.form.get("filename", "").strip()
    try:
        filename = document_object_key(filename)
    except ValueError:
        return "Documento não encontrado", 404
    connection = db()
    person = connection.execute("SELECT documentos FROM pessoas WHERE id = ?", (pid,)).fetchone()
    if not person:
        connection.close()
        if wants_json:
            return jsonify(error="Cadastro não encontrado."), 404
        return "Não encontrado", 404
    if filename not in {item["filename"] for item in document_entries(person["documentos"])}:
        connection.close()
        if wants_json:
            return jsonify(error="Documento não encontrado."), 404
        return "Documento não encontrado", 404
    try:
        delete_document(filename)
    except Exception:
        app.logger.exception(
            "Falha ao excluir o objeto %s do assistido %s; a referência será removida mesmo assim",
            filename, pid,
        )
    connection.execute(
        "UPDATE pessoas SET documentos = ? WHERE id = ?",
        (remove_document_reference(person["documentos"], filename), pid),
    )
    connection.commit()
    connection.close()
    audit("Documento removido do cadastro", "pessoa", pid, tipo_acao="Anexo de Documento")
    if wants_json:
        return jsonify(success=True)
    flash("Documento excluído com sucesso.")
    return redirect(url_for("pessoa", pid=pid))


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
    activity_history = connection.execute(
        "SELECT a.*, COALESCE(a.usuario_nome, u.nome, 'Usuário removido') AS usuario_nome_display "
        "FROM auditoria a LEFT JOIN users u ON u.id = a.usuario_id "
        "WHERE a.assistido_id = ? "
        "OR (a.assistido_id IS NULL AND a.entidade = 'pessoa' AND a.entidade_id = ?) "
        "OR (a.entidade = 'grupo_reflexivo' AND a.entidade_id IN "
        "(SELECT gp.grupo_id FROM grupo_participantes gp WHERE gp.pessoa_id = ?)) "
        "ORDER BY COALESCE(a.data_hora, a.criado_em) DESC, a.id DESC",
        (pid, pid, pid),
    ).fetchall()
    activity_history = [
        {**dict(item), "data_hora_local": format_audit_timestamp(item["data_hora"] or item["criado_em"])}
        for item in activity_history
    ]
    missed_appointments = [appointment for appointment in appointments if appointment["status"] == "Faltou"]
    connection.close()
    if not person:
        return "Não encontrado", 404
    return render_template(
        "pessoa_detalhe.html", person=person, attendances=attendances, frequencies=frequencies,
        appointments=appointments, missed_appointments=missed_appointments,
        scheduling_alerts=scheduling_alerts, documents=document_entries(person["documentos"]),
        activity_history=activity_history,
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
        if not person or professional not in PROFESSIONAL_KEYS or not appointment_date or not appointment_time:
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
    if not person or professional not in PROFESSIONAL_KEYS:
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
    if professional not in PROFESSIONAL_KEYS or not valid_month_day or not valid_times:
        flash("Preencha profissional, dia e um intervalo de horário válido.")
        return redirect(url_for("agendamentos"))
    connection = db()
    availability_columns = table_columns(connection, "disponibilidades")
    if "dia_semana" in availability_columns:
        cursor = connection.execute(
            "INSERT INTO disponibilidades(profissional, dia_semana, dia_mes, hora_inicio, hora_fim, criado_por, criado_em) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (professional, month_day, month_day, start_time, end_time, session["uid"], datetime.now().isoformat(timespec="seconds")),
        )
    else:
        cursor = connection.execute(
            "INSERT INTO disponibilidades(profissional, dia_mes, hora_inicio, hora_fim, criado_por, criado_em) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (professional, month_day, start_time, end_time, session["uid"], datetime.now().isoformat(timespec="seconds")),
        )
    availability_id = cursor.lastrowid
    connection.commit()
    connection.close()
    audit(f"Disponibilidade cadastrada para {professional}, dia {month_day}, {start_time}-{end_time}",
          "disponibilidade", availability_id, tipo_acao="Agendamento")
    flash("Disponibilidade cadastrada com sucesso.")
    return redirect(url_for("agendamentos"))


@app.route("/agendamentos/disponibilidade/<int:availability_id>/excluir", methods=["POST"])
def excluir_disponibilidade(availability_id):
    denial = admin_required()
    if denial:
        return denial
    connection = db()
    availability = connection.execute(
        "SELECT profissional, dia_mes, hora_inicio, hora_fim FROM disponibilidades WHERE id = ?",
        (availability_id,),
    ).fetchone()
    if not availability:
        connection.close()
        return "Disponibilidade não encontrada", 404
    connection.execute("DELETE FROM disponibilidades WHERE id = ?", (availability_id,))
    connection.commit()
    connection.close()
    audit(
        f"Disponibilidade removida: {availability['profissional']}, dia {availability['dia_mes']}, "
        f"{availability['hora_inicio']}-{availability['hora_fim']}",
        "disponibilidade", availability_id, tipo_acao="Agendamento",
    )
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
    try:
        object_key = document_object_key(filename)
    except ValueError:
        return "Não encontrado", 404
    try:
        response = supabase_storage().create_signed_url(object_key, 300)
        signed_url = response.get("signedUrl") or response.get("signedURL")
        if not signed_url:
            raise RuntimeError("Supabase não retornou uma URL assinada")
    except Exception:
        app.logger.exception("Falha ao gerar URL assinada para o documento %s", object_key)
        person_id = document_owner_id(object_key)
        flash(
            "O arquivo físico não foi encontrado no servidor. "
            "Por favor, exclua o registro e faça o re-upload.",
            "warning",
        )
        if person_id:
            return redirect(url_for("pessoa", pid=person_id))
        return redirect(url_for("pessoas"))
    return redirect(signed_url)


def attendance_professional_details(user):
    if not user:
        return "", False
    profile = user["perfil"] or ""
    name = (user["nome"] or "").strip()
    cargo = (user["cargo"] or perfil_nome(profile)).strip()
    council = (user["conselho_regional"] or "").strip()
    council_prefix = PROFESSIONAL_COUNCIL_PREFIXES.get(profile, "")
    if council and council_prefix and not re.search(r"\b(?:CRP|CRESS|OAB)\b", council, re.IGNORECASE):
        council = f"{council_prefix} {council}"
    return ", ".join(part for part in (name, cargo, council) if part), bool(
        name and cargo and council and re.search(r"\d", council)
    )


def attendance_form_values(form):
    values = {field: form.get(field, "").strip() for field in ATTENDANCE_FIELDS}
    values["data"] = values["data_atendimento"]
    values["inicio_fim"] = values["horario_inicio_fim"]
    values["profissional"] = values["profissional_nome_cargo"]
    values["compareceu"] = values["comparecimento_voluntario_opcao"]
    values["busca_ativa"] = values["busca_ativa_opcao"]
    values["emprego"] = values["empregado_estudando_opcao"]
    values["aderencia"] = values["aderencia_medidas_opcao"]
    values["mudou_contato"] = values["mudanca_endereco_contato_opcao"]
    values["relato"] = values["relato_subjetivo"]
    values["intervencao"] = values["intervencao_profissional"]
    values["pendencias"] = values["encaminhamentos_pendentes"]

    recommendations = [
        item for item in form.getlist("recomendacao_selecionada")
        if item in ATTENDANCE_RECOMMENDATIONS
    ]
    values["recomendacoes"] = json.dumps(recommendations, ensure_ascii=False)
    values["recomendacao"] = "; ".join(recommendations)
    conditions = {}
    for key, label in ATTENDANCE_CONDITIONS:
        conditions[key] = {
            "nome": form.get("condicao_outra_nome", "").strip() if key == "outra" else label,
            "status": form.get(f"{key}_status", "") if form.get(f"{key}_status", "") in {"Concluído", "Em curso", "Pendente", "nao_possui"} else "",
            "doc_apresentada": form.get(f"{key}_doc", "") if form.get(f"{key}_doc", "") in {"Sim", "Não"} else "",
            "observacoes": form.get(f"{key}_observacoes", "").strip(),
        }
    values["condicoes_judiciais"] = json.dumps(conditions, ensure_ascii=False)
    values["tipo_retorno"] = form.get("tipo_retorno", "") if form.get("tipo_retorno", "") in ATTENDANCE_RETURN_TYPES else ""
    return values


def attendance_form_data(attendance=None):
    data = dict(attendance) if attendance else {}
    fallback_fields = {
        "data_atendimento": "data", "horario_inicio_fim": "inicio_fim",
        "profissional_nome_cargo": "profissional", "comparecimento_voluntario_opcao": "compareceu",
        "busca_ativa_opcao": "busca_ativa", "empregado_estudando_opcao": "emprego",
        "aderencia_medidas_opcao": "aderencia", "mudanca_endereco_contato_opcao": "mudou_contato",
        "relato_subjetivo": "relato", "intervencao_profissional": "intervencao",
        "encaminhamentos_pendentes": "pendencias",
    }
    for field, legacy in fallback_fields.items():
        data[field] = data.get(field) or data.get(legacy) or ""
    try:
        stored_conditions = json.loads(data.get("condicoes_judiciais") or "{}")
    except (TypeError, ValueError):
        stored_conditions = {}
    conditions = []
    for key, label in ATTENDANCE_CONDITIONS:
        item = stored_conditions.get(key, {}) if isinstance(stored_conditions, dict) else {}
        conditions.append({
            "key": key, "label": label, "nome": item.get("nome", label),
            "status": item.get("status", ""), "doc_apresentada": item.get("doc_apresentada", ""),
            "observacoes": item.get("observacoes", ""),
        })
    try:
        recommendations = json.loads(data.get("recomendacoes") or "[]")
        if not isinstance(recommendations, list):
            recommendations = []
    except (TypeError, ValueError):
        recommendations = []
    if not recommendations and data.get("recomendacao"):
        recommendations = [item.strip() for item in data["recomendacao"].split(";") if item.strip()]
    data.setdefault("profissional_nome_cargo", session.get("nome", ""))
    return data, conditions, recommendations


@app.route("/pessoa/<int:pid>/atendimento", methods=["GET", "POST"])
def novo_atendimento(pid):
    if not authenticated():
        return redirect(url_for("login"))
    connection = db()
    person = connection.execute("SELECT id, nome, processo, medida FROM pessoas WHERE id = ?", (pid,)).fetchone()
    professional_user = connection.execute(
        "SELECT nome, cargo, perfil, conselho_regional FROM users WHERE id = ?", (session["uid"],)
    ).fetchone()
    professional_label, has_professional_details = attendance_professional_details(professional_user)
    if not person:
        connection.close()
        return "Assistido não encontrado", 404
    if request.method == "POST":
        submitted_professional = request.form.get("profissional_nome_cargo", "").strip()
        expected_council_prefix = PROFESSIONAL_COUNCIL_PREFIXES.get(professional_user["perfil"] or "") \
            if professional_user else ""
        profile_name = (professional_user["nome"] or "").strip() if professional_user else ""
        profile_cargo = (professional_user["cargo"] or perfil_nome(professional_user["perfil"])).strip() \
            if professional_user else ""
        has_submitted_identity = profile_name.lower() in submitted_professional.lower() \
            and profile_cargo.lower() in submitted_professional.lower()
        has_submitted_council = bool(
            expected_council_prefix
            and re.search(rf"\b{expected_council_prefix}\b", submitted_professional, re.IGNORECASE)
            and re.search(r"\d", submitted_professional)
        )
        if professional_user and professional_user["perfil"] in MULTIDISCIPLINARY_PROFILES \
                and not has_professional_details and not (has_submitted_council and has_submitted_identity):
            connection.close()
            data, conditions, recommendations = attendance_form_data()
            data["profissional_nome_cargo"] = submitted_professional
            flash("Informe o número do conselho regional (CRP, CRESS ou OAB) no perfil ou no campo profissional.", "warning")
            return render_template(
                "atendimento.html", title="Atendimento Multidisciplinar", person=person,
                attendance=data, conditions=conditions, recommendations=recommendations,
                return_types=ATTENDANCE_RETURN_TYPES, recommendation_options=ATTENDANCE_RECOMMENDATIONS,
                professional_is_complete=has_professional_details,
            )
        values = attendance_form_values(request.form)
        if has_professional_details:
            values["profissional_nome_cargo"] = professional_label
            values["profissional"] = professional_label
        cursor = connection.execute(
            "INSERT INTO atendimentos(pessoa_id, %s, criado_por, criado_em) VALUES (?, %s, ?, ?)" % (
                ", ".join(ATTENDANCE_FIELDS), ", ".join("?" for _ in ATTENDANCE_FIELDS)
            ),
            (pid, *(values[field] for field in ATTENDANCE_FIELDS), session["uid"],
             datetime.now().isoformat(timespec="seconds")),
        )
        attendance_id = cursor.lastrowid
        connection.commit()
        connection.close()
        audit("Atendimento registrado", "atendimento", attendance_id)
        flash("Atendimento registrado com sucesso.")
        return redirect(url_for("pessoa", pid=pid))
    connection.close()
    data, conditions, recommendations = attendance_form_data()
    if professional_label:
        data["profissional_nome_cargo"] = professional_label
    return render_template(
        "atendimento.html", title="Atendimento Multidisciplinar", person=person,
        attendance=data, conditions=conditions, recommendations=recommendations,
        return_types=ATTENDANCE_RETURN_TYPES, recommendation_options=ATTENDANCE_RECOMMENDATIONS,
        professional_is_complete=has_professional_details,
    )


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
        values = attendance_form_values(request.form)
        connection.execute(
            "UPDATE atendimentos SET %s WHERE id = ?" % ", ".join(f"{field} = ?" for field in ATTENDANCE_FIELDS),
            (*(values[field] for field in ATTENDANCE_FIELDS), aid),
        )
        connection.commit()
        connection.close()
        audit("Atendimento atualizado", "atendimento", aid)
        flash("Atendimento atualizado com sucesso.")
        return redirect(url_for("pessoa", pid=attendance["pessoa_id"]))
    connection.close()
    data, conditions, recommendations = attendance_form_data(attendance)
    return render_template(
        "atendimento.html",
        title="Editar Atendimento Multidisciplinar",
        person=get_person(attendance["pessoa_id"]), attendance=data, conditions=conditions,
        recommendations=recommendations, return_types=ATTENDANCE_RETURN_TYPES,
        recommendation_options=ATTENDANCE_RECOMMENDATIONS,
    )


@app.route("/atendimento/<int:aid>")
def visualizar_atendimento(aid):
    if not authenticated():
        return redirect(url_for("login"))
    connection = db()
    attendance = connection.execute(
        "SELECT a.*, p.nome AS pessoa_nome, p.processo, p.medida "
        "FROM atendimentos a JOIN pessoas p ON p.id = a.pessoa_id WHERE a.id = ?", (aid,)
    ).fetchone()
    connection.close()
    if not attendance:
        return "Não encontrado", 404
    data, conditions, recommendations = attendance_form_data(attendance)
    return render_template(
        "atendimento_visualizar.html", attendance=data, person=attendance,
        conditions=conditions, recommendations=recommendations,
    )


def printable(title, body, print_class=""):
    return render_template("printable.html", title=title, body=body, print_class=print_class)


def get_person(person_id):
    connection = db()
    person = connection.execute("SELECT * FROM pessoas WHERE id = ?", (person_id,)).fetchone()
    connection.close()
    return person


@app.route("/termo/<int:pid>")
def termo(pid):
    if not authenticated():
        return redirect(url_for("login"))
    connection = db()
    assistido = connection.execute("SELECT * FROM pessoas WHERE id = ?", (pid,)).fetchone()
    servidor = connection.execute(
        "SELECT nome, cargo, matricula, conselho_regional FROM users WHERE id = ?", (session["uid"],)
    ).fetchone()
    connection.close()
    if not assistido:
        return "Não encontrado", 404

    def assistido_value(*field_names):
        for field_name in field_names:
            if field_name in assistido.keys() and assistido[field_name]:
                return assistido[field_name]
        return "—"

    assistido_termo = {
        "nome": assistido_value("nome"),
        "nome_social": assistido_value("nome_social"),
        "cpf": assistido_value("cpf"),
        "rg": assistido_value("rg"),
        "nome_mae": assistido_value("nome_mae"),
        "numero_processo": assistido_value("numero_processo", "processo"),
        "rji": assistido_value("rji", "numero_inscricao", "id_unico"),
        "orgao_judicial": assistido_value("orgao_judicial", "vara", "vara_comarca"),
        "telefone": assistido_value("telefone"),
    }
    return printable(
        "Termo de Atendimento",
        render_template(
            "termo.html", assistido=assistido_termo, servidor=servidor,
        ),
        print_class="term-print-page",
    )


@app.route("/retorno/<int:aid>")
def retorno(aid):
    if not authenticated():
        return redirect(url_for("login"))
    connection = db()
    attendance = connection.execute(
        "SELECT a.*, p.nome, p.processo, p.medida, u.nome AS servidor_nome, u.cargo AS servidor_cargo, "
        "u.matricula AS servidor_matricula, u.conselho_regional AS servidor_conselho_regional "
        "FROM atendimentos a JOIN pessoas p ON p.id = a.pessoa_id "
        "LEFT JOIN users u ON u.id = a.criado_por WHERE a.id = ?", (aid,)
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


@app.route("/pessoa/<int:pid>/declaracao-comparecimento")
def declaracao_comparecimento(pid):
    if not authenticated():
        return redirect(url_for("login"))
    person = get_person(pid)
    if not person:
        return "Não encontrado", 404
    connection = db()
    servidor = connection.execute(
        "SELECT nome, cargo, matricula, conselho_regional FROM users WHERE id = ?", (session["uid"],)
    ).fetchone()
    connection.close()
    return printable(
        "Declaração de Comparecimento",
        render_template("declaracao_comparecimento.html", person=person, servidor=servidor),
        print_class="attendance-declaration-page",
    )


@app.route("/grupos")
def grupos():
    if not authenticated():
        return redirect(url_for("login"))
    connection = db()
    available_people = connection.execute(
        "SELECT id, nome, processo, grupo_responsabilizacao FROM pessoas "
        "WHERE grupo_responsabilizacao <> '' AND id NOT IN "
        "(SELECT pessoa_id FROM grupo_participantes) ORDER BY nome"
    ).fetchall()
    group_rows = connection.execute(
        "SELECT id, nome, status, criado_em FROM grupos_reflexivos ORDER BY id DESC"
    ).fetchall()
    participant_rows = connection.execute(
        "SELECT gp.grupo_id, gp.pessoa_id, p.nome, p.cpf, p.processo, p.grupo_responsabilizacao "
        "FROM grupo_participantes gp JOIN pessoas p ON p.id = gp.pessoa_id "
        "ORDER BY gp.grupo_id DESC, p.nome"
    ).fetchall()
    group_frequency_rows = connection.execute(
        "SELECT grupo_id, pessoa_id, encontro, status, data, horario, facilitadores FROM grupo_frequencias "
        "ORDER BY grupo_id, encontro"
    ).fetchall()
    facilitators = connection.execute(
        "SELECT nome, cargo FROM users WHERE ativo = 1 AND status = 'aprovado' ORDER BY nome"
    ).fetchall()
    connection.close()
    participants_by_group = {}
    for participant in participant_rows:
        participants_by_group.setdefault(participant["grupo_id"], []).append(dict(participant))
    frequencies_by_participant = {}
    for frequency in group_frequency_rows:
        frequencies_by_participant.setdefault(
            (frequency["grupo_id"], frequency["pessoa_id"]), {}
        )[frequency["encontro"]] = dict(frequency)
    groups = []
    for group in group_rows:
        group_data = dict(group)
        group_data["participants"] = participants_by_group.get(group["id"], [])
        for participant in group_data["participants"]:
            participant["frequencies"] = frequencies_by_participant.get(
                (group["id"], participant["pessoa_id"]), {}
            )
        group_data["frequency_data"] = {
            str(participant["pessoa_id"]): {
                str(encounter): frequency
                for encounter, frequency in participant["frequencies"].items()
            }
            for participant in group_data["participants"]
        }
        registered = {
            encounter: frequency
            for participant in group_data["participants"]
            for encounter, frequency in participant["frequencies"].items()
            if frequency["status"]
        }
        group_data["last_encounter"] = max(registered) if registered else 0
        group_data["recorded_count"] = len(registered)
        groups.append(group_data)
    return render_template(
        "grupos.html", groups=groups, available_people=available_people, facilitators=facilitators
    )


@app.route("/grupos/formar", methods=["POST"])
def formar_grupo():
    denial = admin_required()
    if denial:
        return denial
    person_ids = list(dict.fromkeys(request.form.getlist("pessoa_id")))
    if not 1 <= len(person_ids) <= 20:
        flash("Selecione entre 1 e 20 assistidos para formar o grupo.")
        return redirect(url_for("grupos"))
    group_name = request.form.get("nome", "").strip()
    connection = db()
    placeholders = ",".join("?" for _ in person_ids)
    people = connection.execute(
        f"SELECT id FROM pessoas WHERE id IN ({placeholders}) AND grupo_responsabilizacao <> ''",
        person_ids,
    ).fetchall()
    already_grouped = connection.execute(
        f"SELECT pessoa_id FROM grupo_participantes WHERE pessoa_id IN ({placeholders})",
        person_ids,
    ).fetchall()
    if len(people) != len(person_ids) or already_grouped:
        connection.close()
        flash("Um ou mais assistidos já pertencem a um grupo ou não podem ser selecionados.")
        return redirect(url_for("grupos"))
    if not group_name:
        next_number = connection.execute("SELECT COUNT(*) AS total FROM grupos_reflexivos").fetchone()["total"] + 1
        group_name = f"Grupo reflexivo {next_number}"
    group_cursor = connection.execute(
        "INSERT INTO grupos_reflexivos(nome, status, criado_em, criado_por) VALUES (?, ?, ?, ?)",
        (group_name, "Em andamento", datetime.now().isoformat(timespec="seconds"), session["uid"]),
    )
    group_id = group_cursor.lastrowid
    for person_id in person_ids:
        connection.execute(
            "INSERT INTO grupo_participantes(grupo_id, pessoa_id) VALUES (?, ?)",
            (group_id, person_id),
        )
    connection.commit()
    connection.close()
    audit(f"Grupo reflexivo formado: {group_name}", "grupo_reflexivo", group_id)
    flash(f"{group_name} formado com {len(person_ids)} assistidos.")
    return redirect(url_for("grupos"))


@app.route("/grupos/<int:group_id>/editar", methods=["GET", "POST"])
def editar_grupo(group_id):
    denial = admin_required()
    if denial:
        return denial
    connection = db()
    group = connection.execute("SELECT id, nome FROM grupos_reflexivos WHERE id = ?", (group_id,)).fetchone()
    if not group:
        connection.close()
        return "Grupo não encontrado", 404
    if request.method == "POST":
        new_name = request.form.get("nome", "").strip()
        if not new_name:
            flash("Informe um nome para o grupo.")
            connection.close()
            return render_template("grupo_form.html", title="Editar grupo", group=group)
        connection.execute("UPDATE grupos_reflexivos SET nome = ? WHERE id = ?", (new_name, group_id))
        connection.commit()
        connection.close()
        audit(f"Grupo reflexivo atualizado: {new_name}", "grupo_reflexivo", group_id)
        flash("Grupo atualizado com sucesso.")
        return redirect(url_for("grupos"))
    connection.close()
    return render_template("grupo_form.html", title="Editar grupo", group=group)


@app.route("/grupos/<int:group_id>/excluir", methods=["POST"])
def excluir_grupo(group_id):
    denial = admin_required()
    if denial:
        return denial
    connection = db()
    group = connection.execute("SELECT id, nome FROM grupos_reflexivos WHERE id = ?", (group_id,)).fetchone()
    if not group:
        connection.close()
        return "Grupo não encontrado", 404
    affected_people = connection.execute(
        "SELECT p.id, p.nome FROM grupo_participantes gp JOIN pessoas p ON p.id = gp.pessoa_id "
        "WHERE gp.grupo_id = ?", (group_id,),
    ).fetchall()
    connection.execute("DELETE FROM grupo_frequencias WHERE grupo_id = ?", (group_id,))
    connection.execute("DELETE FROM grupo_participantes WHERE grupo_id = ?", (group_id,))
    connection.execute("DELETE FROM grupos_reflexivos WHERE id = ?", (group_id,))
    connection.commit()
    connection.close()
    if affected_people:
        for affected_person in affected_people:
            audit(f"Grupo reflexivo excluído: {group['nome']}", "grupo_reflexivo", group_id,
                  assistido_id=affected_person["id"], assistido_nome=affected_person["nome"])
    else:
        audit(f"Grupo reflexivo excluído: {group['nome']}", "grupo_reflexivo", group_id)
    flash("Grupo excluído com sucesso.")
    return redirect(url_for("grupos"))


@app.route("/grupos/<int:group_id>/frequencia", methods=["POST"])
def salvar_frequencia_grupo(group_id):
    denial = admin_required()
    if denial:
        return denial
    try:
        encounter = int(request.form.get("encontro", ""))
    except ValueError:
        encounter = 0
    attendance_date = request.form.get("data", "").strip()
    attendance_time = request.form.get("horario", "").strip()
    facilitator_names = request.form.getlist("facilitador")
    facilitator_names = [name.strip() for name in facilitator_names if name.strip()]
    facilitators = ", ".join(dict.fromkeys(facilitator_names))
    if encounter not in range(1, 11):
        flash("O encontro deve estar entre 1 e 10.")
        return redirect(url_for("grupos"))
    connection = db()
    group = connection.execute("SELECT id, nome FROM grupos_reflexivos WHERE id = ?", (group_id,)).fetchone()
    participants = connection.execute(
        "SELECT pessoa_id FROM grupo_participantes WHERE grupo_id = ?", (group_id,)
    ).fetchall()
    if not group:
        connection.close()
        return "Grupo não encontrado", 404
    for participant in participants:
        person_id = participant["pessoa_id"]
        status = request.form.get(f"frequencia_status_{person_id}", "")
        if status not in FREQUENCY_STATUS_OPTIONS:
            connection.close()
            flash("Status de frequência inválido.")
            return redirect(url_for("grupos"))
        connection.execute(
            "INSERT INTO grupo_frequencias(grupo_id, pessoa_id, encontro, status, data, horario, facilitadores) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(grupo_id, pessoa_id, encontro) DO UPDATE SET status = excluded.status, "
            "data = excluded.data, horario = excluded.horario, facilitadores = excluded.facilitadores",
            (group_id, person_id, encounter, status, attendance_date, attendance_time, facilitators),
        )
        connection.execute(
            "INSERT INTO frequencias(pessoa_id, encontro, status, data) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(pessoa_id, encontro) DO UPDATE SET status = excluded.status, data = excluded.data",
            (person_id, encounter, status, attendance_date),
        )
        absence_count = connection.execute(
            "SELECT COUNT(*) AS total FROM frequencias WHERE pessoa_id = ? AND status = 'Faltou'",
            (person_id,),
        ).fetchone()["total"]
        group_status = "Eliminado - refazer grupo" if absence_count >= 3 else "Em andamento"
        alert = (
            "ALERTA: 3 ou mais faltas. Assistido eliminado e deve refazer o grupo."
            if absence_count >= 3 else
            "ALERTA: 2 faltas registradas. Na próxima falta, o Assistido será eliminado e deverá refazer o grupo."
            if absence_count == 2 else ""
        )
        connection.execute(
            "UPDATE pessoas SET situacao_grupo = ?, alerta_frequencia = ? WHERE id = ?",
            (group_status, alert, person_id),
        )
    connection.commit()
    connection.close()
    audit(f"Frequência do {encounter}º encontro atualizada", "grupo_reflexivo", group_id)
    flash(f"Frequência do {encounter}º encontro salva.")
    return redirect(url_for("grupos"))


@app.route("/grupos/<int:group_id>/frequencia", methods=["GET"])
def historico_frequencia_grupo(group_id):
    if not authenticated():
        return redirect(url_for("login"))
    connection = db()
    group = connection.execute("SELECT id FROM grupos_reflexivos WHERE id = ?", (group_id,)).fetchone()
    if not group:
        connection.close()
        return jsonify({"error": "Grupo não encontrado"}), 404
    rows = connection.execute(
        "SELECT grupo_id, pessoa_id, encontro, status, data, horario, facilitadores "
        "FROM grupo_frequencias WHERE grupo_id = ? ORDER BY encontro, pessoa_id",
        (group_id,),
    ).fetchall()
    connection.close()
    return jsonify([dict(row) for row in rows])


@app.route("/grupos/<int:group_id>/frequencia/imprimir")
def imprimir_frequencia_grupo(group_id):
    if not authenticated():
        return redirect(url_for("login"))
    try:
        encounter = int(request.args.get("encontro", "1"))
    except ValueError:
        encounter = 0
    if encounter not in range(1, 11):
        return "Encontro inválido", 400
    connection = db()
    group = connection.execute(
        "SELECT id, nome FROM grupos_reflexivos WHERE id = ?", (group_id,)
    ).fetchone()
    participants = connection.execute(
        "SELECT p.nome, p.cpf FROM grupo_participantes gp JOIN pessoas p ON p.id = gp.pessoa_id "
        "WHERE gp.grupo_id = ? ORDER BY p.nome", (group_id,)
    ).fetchall()
    meeting = connection.execute(
        "SELECT data, horario, facilitadores FROM grupo_frequencias "
        "WHERE grupo_id = ? AND encontro = ? ORDER BY id LIMIT 1", (group_id, encounter)
    ).fetchone()
    connection.close()
    if not group:
        return "Grupo não encontrado", 404
    return printable(
        f"Lista de frequência - {group['nome']}",
        render_template(
            "frequencia_grupo.html", group=group, participants=participants,
            encounter=encounter, meeting=meeting,
        ),
        print_class="group-frequency-print-page",
    )


@app.route("/auditoria")
def auditoria():
    if not authenticated():
        return redirect(url_for("login"))
    filters = {
        "usuario": request.args.get("usuario", "").strip(),
        "tipo_acao": request.args.get("tipo_acao", "").strip(),
        "assistido": request.args.get("assistido", "").strip(),
        "data_inicial": request.args.get("data_inicial", "").strip(),
        "data_final": request.args.get("data_final", "").strip(),
    }
    try:
        page = max(1, int(request.args.get("pagina", "1")))
    except ValueError:
        page = 1
    per_page = 30
    clauses = ["1 = 1"]
    parameters = []
    if filters["usuario"]:
        try:
            parameters.append(int(filters["usuario"]))
        except ValueError:
            parameters.append(-1)
        clauses.append("a.usuario_id = ?")
    if filters["tipo_acao"]:
        clauses.append("COALESCE(a.tipo_acao, a.acao) = ?")
        parameters.append(filters["tipo_acao"])
    if filters["assistido"]:
        clauses.append("(COALESCE(a.assistido_nome, p.nome, '') LIKE ? OR COALESCE(p.cpf, '') LIKE ?)")
        parameters.extend([f"%{filters['assistido']}%", f"%{filters['assistido']}%"])
    if filters["data_inicial"]:
        try:
            start_date = date.fromisoformat(filters["data_inicial"])
        except ValueError:
            filters["data_inicial"] = ""
        else:
            clauses.append("COALESCE(a.data_hora, a.criado_em) >= ?")
            parameters.append(f"{start_date.isoformat()}T00:00:00")
    if filters["data_final"]:
        try:
            end_exclusive = date.fromisoformat(filters["data_final"]) + timedelta(days=1)
        except ValueError:
            filters["data_final"] = ""
        else:
            clauses.append("COALESCE(a.data_hora, a.criado_em) < ?")
            parameters.append(f"{end_exclusive.isoformat()}T00:00:00")
    where_sql = " AND ".join(clauses)
    connection = db()
    total = connection.execute(
        "SELECT COUNT(*) AS total FROM auditoria a LEFT JOIN pessoas p ON p.id = a.assistido_id "
        f"WHERE {where_sql}", parameters,
    ).fetchone()["total"]
    page_count = max(1, (total + per_page - 1) // per_page)
    page = min(page, page_count)
    rows = connection.execute(
        "SELECT a.*, COALESCE(a.usuario_nome, u.nome, 'Usuário removido') AS responsavel, "
        "COALESCE(a.assistido_nome, p.nome, '') AS assistido_display, p.cpf AS assistido_cpf "
        "FROM auditoria a LEFT JOIN users u ON u.id = a.usuario_id "
        "LEFT JOIN pessoas p ON p.id = a.assistido_id "
        f"WHERE {where_sql} ORDER BY COALESCE(a.data_hora, a.criado_em) DESC, a.id DESC "
        "LIMIT ? OFFSET ?",
        [*parameters, per_page, (page - 1) * per_page],
    ).fetchall()
    rows = [
        {**dict(row), "data_hora_local": format_audit_timestamp(row["data_hora"] or row["criado_em"])}
        for row in rows
    ]
    professionals = connection.execute(
        "SELECT DISTINCT a.usuario_id, COALESCE(a.usuario_nome, u.nome, 'Usuário removido') AS nome "
        "FROM auditoria a LEFT JOIN users u ON u.id = a.usuario_id "
        "WHERE a.usuario_id IS NOT NULL ORDER BY nome"
    ).fetchall()
    action_types = connection.execute(
        "SELECT DISTINCT COALESCE(tipo_acao, acao) AS tipo_acao FROM auditoria ORDER BY tipo_acao"
    ).fetchall()
    connection.close()
    return render_template(
        "auditoria.html", rows=rows, filters=filters, professionals=professionals,
        action_types=action_types, page=page, page_count=page_count, total=total,
    )


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
