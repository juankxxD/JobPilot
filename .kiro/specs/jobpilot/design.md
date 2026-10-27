# JobPilot — Design

## Visión general

JobPilot es una aplicación de línea de comandos y un pipeline por lotes. No es un
servicio de larga duración: cada corrida (en GitHub Actions o manual) ejecuta el
ciclo completo, procesa las respuestas pendientes de Telegram y termina. El estado
vive en Postgres (Neon) y en `profile.yaml`.

```
                 ┌─────────────────────────────────────────────────────────┐
                 │                      GitHub Actions (cron 2h)             │
                 │                 o  `jobpilot run`  manual                 │
                 └───────────────────────────┬─────────────────────────────┘
                                             │
        ┌────────────────────────────────────▼──────────────────────────────┐
        │                            Pipeline                                 │
        │  collect → dedupe → prefilter → analyze → score → select →          │
        │  render → upload → propose → process_telegram_updates → follow_up   │
        └───┬──────────┬──────────┬──────────┬──────────┬──────────┬──────────┘
            │          │          │          │          │          │
      JobSource[]   Filters    LlmClient  FileStorage  Notifier  JobRepository
      (fetchers)   (config)   (Groq/Ollama) (Drive)   (Telegram)  (Neon)
                                            Mailer (Graph/Gmail)  FormFiller (local)
```

### Decisiones de arquitectura (resuelven las ambigüedades de requirements)

| # | Decisión | Justificación |
|---|----------|---------------|
| 1 | **uv** para dependencias; exportar `requirements.txt` para CI | Instalación rápida, lockfile reproducible, nativo con `pyproject.toml`. CI usa el export para no depender de uv. |
| 2 | **SQL versionado** (`migrations/NNN_name.sql` + runner propio) | Un solo dueño, esquema pequeño y estable. Alembic añade peso innecesario. Se puede migrar a Alembic luego. |
| 3 | `MAIL_PROVIDER=outlook` por **default**; Graph API principal, Gmail secundaria | Coincide con la sección de stack. El flujo "enviar por correo" usa el provider activo. |
| 4 | **WeasyPrint**; deps de sistema por apt en CI, documentadas por OS en local | PDF ATS con control total de layout vía HTML/CSS. |
| 5 | Default `GROQ_MODEL=openai/gpt-oss-120b` en `.env.example` | `llama-3.3-70b-versatile` fue retirado del tier free/developer de Groq (ago 2026); `openai/gpt-oss-120b` es el reemplazo self-serve recomendado. Configurable. |
| 6 | **lingua-language-detector** + confirmación LLM en casos dudosos | Más preciso que langdetect en descripciones cortas/mezcladas. |
| 7 | Restricción de "años en tecnología" aplica **solo a formularios** | El análisis interno sí deriva años; nunca se auto-responde en un formulario. |
| 8 | Sin respuesta de idioma → oferta queda `proposed` esperando botón; `DEFAULT_LANGUAGE` solo se usa si el dueño lo habilita explícitamente en config | Evita generar documentos en un idioma equivocado. |

> Estas decisiones quedan registradas también en `docs/decisions.md`.

---

## Estructura del repositorio

```
jobpilot/
├─ README.md
├─ pyproject.toml
├─ requirements.txt            # exportado desde uv.lock para CI
├─ config.yaml                 # reglas de prefiltro, pesos de scoring, flags
├─ .env.example
├─ profile.example.yaml
├─ profile.yaml                # (gitignored) fuente de verdad real
├─ .gitignore
├─ migrations/
│  └─ 001_init.sql
├─ src/jobpilot/
│  ├─ __init__.py
│  ├─ __main__.py              # entrypoint `python -m jobpilot`
│  ├─ cli.py                   # Typer app: run, import-cv, auth-mail, check-*, ...
│  ├─ config.py                # carga config.yaml + env (Pydantic Settings)
│  ├─ logging.py               # logging estructurado (structlog)
│  ├─ interfaces.py            # Protocols: LlmClient, JobSource, FileStorage,
│  │                           #   Notifier, Mailer, JobRepository, FormFiller
│  ├─ models.py                # modelos de dominio Pydantic compartidos
│  ├─ profile/
│  │  ├─ model.py              # ProfileModel (Pydantic v2) + validación
│  │  ├─ importer.py           # extracción PDF/DOCX + prompt a draft
│  │  └─ validation.py         # chequeo de ids, idiomas aprobados
│  ├─ fetchers/
│  │  ├─ base.py               # JobSource ABC + registro
│  │  ├─ getonboard.py
│  │  ├─ remoteok.py
│  │  ├─ remotive.py
│  │  ├─ weworkremotely.py     # RSS
│  │  ├─ himalayas.py          # flag
│  │  ├─ adzuna.py             # flag
│  │  └─ gmail_alerts.py       # flag (LinkedIn/Indeed/Google Jobs vía Gmail)
│  ├─ filters/
│  │  └─ prefilter.py          # reglas deterministas desde config
│  ├─ llm/
│  │  ├─ client.py             # GroqClient, OllamaClient, FallbackLlmClient
│  │  ├─ prompts.py            # plantillas con reglas anti-invención
│  │  └─ schemas.py            # esquemas Pydantic de cada salida LLM
│  ├─ language.py              # detección (lingua) + confirmación LLM
│  ├─ scoring/
│  │  └─ scorer.py             # análisis + scoring + umbral
│  ├─ documents/
│  │  ├─ selector.py           # el LLM elige variante/ids; validación por código
│  │  ├─ render.py             # HTML→PDF con WeasyPrint
│  │  └─ templates/
│  │     ├─ cv_es.html, cv_en.html
│  │     ├─ cover_es.html, cover_en.html
│  │     └─ styles.css
│  ├─ storage/
│  │  ├─ neon.py               # JobRepository con psycopg 3 + retry
│  │  ├─ migrate.py            # runner de migraciones SQL
│  │  └─ drive.py              # FileStorage con Google Drive
│  ├─ channels/
│  │  └─ telegram.py           # Notifier + getUpdates + botones + comandos
│  ├─ mail/
│  │  ├─ base.py               # Mailer ABC
│  │  ├─ graph.py              # Outlook/Hotmail vía Microsoft Graph (default)
│  │  └─ gmail.py              # Gmail API
│  ├─ forms/                   # Fase 8 (local)
│  │  ├─ base.py               # FormFiller ABC + detección de plataforma
│  │  ├─ greenhouse.py
│  │  └─ lever.py
│  └─ pipeline/
│     ├─ run.py                # orquestación del ciclo completo
│     └─ follow_up.py          # recordatorios y reporte semanal
├─ scripts/
│  └─ check_*.py               # verificación de credenciales (Fase 0)
├─ tests/
│  ├─ conftest.py
│  ├─ fixtures/                # ofertas es/en, respuestas LLM, PDFs mínimos
│  └─ test_*.py
├─ .github/workflows/pipeline.yml
└─ docs/
   ├─ architecture.md
   ├─ decisions.md
   └─ deployment.md
```

---

## Interfaces (Protocols)

Se usan `typing.Protocol` para desacoplar y poder mockear en tests.

```python
class LlmClient(Protocol):
    def complete_json(
        self, prompt: str, schema: type[BaseModel], *, max_retries: int = 1
    ) -> BaseModel: ...


class JobSource(Protocol):
    name: str
    enabled: bool

    def fetch(self) -> Iterable[RawJob]: ...


class FileStorage(Protocol):
    def upload(self, path: Path, name: str) -> StoredFile: ...  # devuelve web_view_link


class Notifier(Protocol):
    def send_proposal(self, proposal: Proposal) -> None: ...
    def get_updates(self, offset: int | None) -> list[Update]: ...
    def send_message(self, text: str, buttons: list[Button] | None = None) -> None: ...


class Mailer(Protocol):
    def send(self, to: str, subject: str, body: str, attachments: list[Attachment]) -> None: ...


class JobRepository(Protocol):
    def upsert_job(self, job: Job) -> Job: ...
    def set_status(self, job_id: int, status: JobStatus) -> None: ...
    def create_application(self, app: Application) -> Application: ...
    def get_state(self, key: str) -> str | None: ...
    def set_state(self, key: str, value: str) -> None: ...

    # ... consultas de pendientes, por empresa+cargo, etc.


class FormFiller(Protocol):
    def matches(self, url: str) -> bool: ...
    def fill(self, url: str, ctx: FillContext) -> FillResult: ...  # no envía
```

El **FallbackLlmClient** compone `GroqClient` y `OllamaClient`: intenta Groq;
ante error de red, cuota (429) o esquema inválido tras el reintento, cae a Ollama.

---

## Modelos de dominio (Pydantic v2)

- `RawJob`: salida cruda de un fetcher (campos normalizados básicos).
- `Job`: entidad persistida (coincide con tabla `jobs`).
- `JobAnalysis`: título normalizado, seniority, stack obligatorio/deseable, años,
  idioma, modalidad, zona horaria, salario, correo contacto, plataforma, red flags.
- `JobScore`: score (0–100), reasons, met_requirements (con achievement_id),
  missing_requirements, recommendation (`apply|apply_with_reservations|discard`).
- `ContentSelection`: summary_variant, ordered_achievement_ids, project_ids,
  highlighted_skill_ids, language.
- `Application`: coincide con tabla `applications`.
- `Proposal`: vista para Telegram (job + score + rutas de documentos + idioma).

### Esquema del perfil (`ProfileModel`)

```yaml
basics: { name, email, phone, location, links: {...} }
summary_variants:
  backend_java: { es: "...", en: "..." }
  fullstack:    { es: "...", en: "..." }
  cloud:        { es: "...", en: "..." }
skills: { languages: [...], frameworks: [...], cloud: [...], databases: [...] }
experience:
  - id: exp_acme_2022
    company: "ACME"
    role: "Backend Engineer"
    start: "2022-01"
    end: "2024-06"           # o null = actual
    achievements:
      - id: ach_acme_latency
        tags: [java, performance]
        es: "Reduje la latencia ..."
        en: "Reduced latency ..."
        translation_status: approved   # approved | pending_review
education: [...]
certifications: [...]
projects:
  - { id: prj_x, name, tags: [...], es: "...", en: "...", translation_status }
languages_spoken: [ { language: "English", level: "C1" } ]
answers_bank:
  - key: salary_expectation
    question_patterns: ["salary", "pretensión salarial", ...]
    sensitive: true
    es: "..."
    en: "..."
preferences:
  target_roles: [...]
  countries: [...]
  remote: true
  min_salary: 0
  accepted_languages: [es, en]
```

**Validación por código** (`profile/validation.py`):
- Todo `id` referenciado por el LLM debe existir en el perfil.
- Un texto en un idioma solo se usa si su `translation_status == approved`.
- Toda tecnología mencionada en carta/respuestas debe estar en `skills` o en los
  `tags` del perfil; si no, se rechaza y se reintenta/marca `error`.

---

## Flujo del pipeline (`pipeline/run.py`)

1. **collect**: para cada `JobSource.enabled`, llamar `fetch()` dentro de un
   try/except; registrar fallos por fuente y continuar. Cada `RawJob` se normaliza.
2. **dedupe + persist**: `upsert_job` con constraint `(source, external_id)` y `url`
   único → idempotente. Ofertas nuevas quedan en estado `new`.
3. **prefilter**: aplicar reglas de `config.yaml`; las que no pasan → `filtered_out`.
4. **language**: detectar idioma con lingua; si confianza baja → confirmación LLM;
   si sigue dudoso → preguntar por Telegram (botones) y dejar en espera.
5. **analyze + score**: por oferta `new` que pasó prefiltro, llamar LLM (con
   fallback). Validar JSON contra esquema; un reintento si falla. Guardar
   `analysis`, `score`, `score_reason`, `missing_requirements`; estado → `scored`.
6. **threshold**: solo `score >= MIN_SCORE` continúan.
7. **guards**: respetar `DAILY_APPLICATION_LIMIT` y no repetir empresa+cargo dentro
   del periodo configurable.
8. **select + render**: LLM elige contenido; validación por código; render CV+carta.
9. **upload**: subir a Drive, guardar `web_view_link`.
10. **propose**: enviar propuesta por Telegram con adjuntos y botones; estado →
    `proposed`.
11. **process_telegram_updates**: `getUpdates` desde `telegram_state.last_update_id`;
    aplicar acciones (Enviar/Ajustar/Descartar/Ya apliqué/Español/Inglés) y comandos.
    Guardar nuevo `last_update_id`. Idempotente por `update_id`.
12. **follow_up**: recordatorios a los 7 días; reporte semanal.

Cada paso es reanudable: el estado del `Job` determina qué hacer en la siguiente
corrida, así una interrupción no duplica trabajo.

---

## LLM: prompts y validación

- Todos los prompts incluyen el bloque **anti-invención** (reglas de requirements).
- Toda salida es JSON validado con el esquema Pydantic correspondiente
  (`llm/schemas.py`). `complete_json` reintenta una vez si el parseo falla,
  añadiendo el error al prompt. Si falla de nuevo → excepción → fallback a Ollama;
  si ambos fallan → la oferta se marca `error`.
- Prompts principales: `import_cv`, `analyze_job`, `score_job`, `select_content`,
  `write_cover_letter`, `confirm_language`, `answer_open_question` (formularios,
  nunca para preguntas sensibles).

---

## Documentos y PDF

- Plantillas HTML separadas por idioma con encabezados localizados. CSS en una
  sola columna, sin tablas/imágenes/íconos, fuentes comunes.
- `render.py` convierte HTML→PDF con WeasyPrint.
- Carta ≤ 200 palabras (validado por código antes de renderizar).
- Nombre de archivo: `Nombre_Apellido_Empresa_Cargo_{ES|EN}.pdf` (slug seguro).
- **Prueba ATS**: extraer texto del PDF (pdfmin.six/pypdf) y verificar que los
  encabezados aparecen en el orden esperado, en ambos idiomas.

---

## Base de datos (Neon / psycopg 3)

- Conexión con retry exponencial (Neon puede estar dormido): reintentar
  `OperationalError`/timeout N veces antes de fallar.
- Migraciones: `storage/migrate.py` ejecuta en orden los `migrations/NNN_*.sql` y
  registra los aplicados en una tabla `schema_migrations`.
- `001_init.sql` crea `jobs`, `applications`, `telegram_state`, `schema_migrations`
  con los constraints de idempotencia.

---

## Telegram

- Un solo chat autorizado (`TELEGRAM_OWNER_CHAT_ID`); se ignora cualquier otro.
- Modo **getUpdates** por corrida (apto para CI). El `last_update_id` se guarda en
  `telegram_state`; cada `update_id` se procesa una sola vez (idempotencia).
- Botones (inline keyboard) codifican `callback_data` con acción + job_id.
- Comandos: `/pendientes`, `/estado`, `/entrevista id`, `/rechazo id`, `/buscar`.
- Documentación para migrar a **long polling** en una VM: misma capa `telegram.py`,
  solo cambia el bucle que llama `get_updates` (continuo vs. una vez).

---

## Correo (Mailer)

- `MAIL_PROVIDER` selecciona la implementación (`outlook` default | `gmail`).
- **graph.py** (Outlook/Hotmail): OAuth 2.0 con **msal**, permiso delegado
  `Mail.Send`, endpoint `sendMail`, `saveToSentItems=true`. Consentimiento inicial
  por `jobpilot auth-mail` (device code flow) que guarda el refresh token; en
  ejecución se usa el refresh token desde env.
- **gmail.py**: Gmail API con OAuth. CV adjunto en PDF.
- SMTP+contraseña: no incluido por defecto; solo documentado como alternativa
  desactivada.

---

## Formularios (Fase 8, local)

- `forms/base.py`: `FormFiller` ABC + `detect_platform(url, dom)`.
- `greenhouse.py`, `lever.py`: localizan campos, llenan desde perfil, suben CV+carta,
  responden preguntas abiertas con LLM (solo perfil+oferta), marcan dudosos.
- **Preguntas sensibles**: solo desde `answers_bank`; sin respuesta → campo vacío +
  aviso. Nunca LLM.
- Antes de enviar: captura + lista de dudosos por Telegram con botones Enviar/
  Cancelar. Solo con Enviar se da el clic final y se registra.
- CAPTCHA / anti-bot → detener y avisar. Estructura inesperada → abortar seguro,
  guardar captura, notificar. Nunca enviar incompleto.
- Playwright con navegador visible configurable; solo PC local, nunca CI.

---

## Configuración y secretos

- `config.py`: `Settings` (Pydantic Settings) lee env; `AppConfig` lee `config.yaml`
  (pesos de scoring, reglas de prefiltro, feature flags, periodos).
- `.env.example` lista todas las variables; `profile.yaml` y `.env` en `.gitignore`.
- En CI, los secretos vienen de GitHub Actions secrets.

---

## Logging y manejo de errores

- **structlog** para logging estructurado (JSON en CI, consola en local).
- Errores por fuente/por oferta aislados: el pipeline continúa; cada falla queda en
  el log y, cuando aplica, en el estado `error` del `Job`.

---

## Testing

- pytest con mocks de todos los servicios externos (LLM, Drive, Telegram, Neon,
  Mailer) vía fakes que implementan los Protocols.
- Fixtures de ofertas en es y en, respuestas LLM de ejemplo, PDFs mínimos.
- Pruebas clave: validación de perfil, prefiltro, parseo/validación de salidas LLM,
  selección con ids inexistentes (debe fallar), idempotencia de upsert y de
  `update_id`, orden de lectura del PDF ATS (es/en), guards de límite diario y
  empresa+cargo.

---

## Mapa fases → módulos

| Fase | Módulos principales | Criterio de terminado |
|------|---------------------|------------------------|
| 0 | repo, config, `scripts/check_*`, `.env.example`, `profile.example.yaml` | cada script confirma conexión |
| 1 | `profile/*`, `cli import-cv` | importo mi CV y obtengo draft bilingüe + reporte |
| 2 | `fetchers/*`, `filters`, `storage/neon` | una corrida llena `jobs` sin duplicados |
| 3 | `llm/*`, `language`, `scoring` | salidas válidas y puntajes coherentes (fixtures es/en) |
| 4 | `documents/*`, `storage/drive` | CV legible por parser ATS es/en + enlace guardado |
| 5 | `channels/telegram`, `pipeline/run` | flujo hasta aprobación y registro |
| 6 | `mail/*`, modo manual | una postulación end-to-end registrada |
| 7 | `pipeline/follow_up` | recordatorios, comandos, reporte semanal |
| 8 | `forms/*` (local) | formulario real llenado hasta paso previo al envío + captura |
| 9 | `docs/*`, `README`, `.github/workflows/pipeline.yml` | documentación y despliegue |
