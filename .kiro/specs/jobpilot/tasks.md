# JobPilot — Implementation Plan

Cada fase termina con su criterio de terminado. No se avanza a la siguiente fase
sin tu aprobación explícita. Cada tarea lista las pruebas unitarias con mocks
requeridas y lo que debes configurar manualmente.

---

## Fase 0 — Preparación

- [ ] 0.1 Inicializar proyecto con uv: `pyproject.toml` (Python 3.12, deps base,
  ruff, pytest, mypy), `.gitignore` (incluye `profile.yaml`, `.env`, `*.pdf`
  generados), exportar `requirements.txt`.
  - _Req: R16.1, R16.5, R17.1_
- [ ] 0.2 Estructura de paquetes `src/jobpilot/*` con `__init__.py`, `__main__.py`
  y `cli.py` (Typer) con subcomandos vacíos (`run`, `import-cv`, `auth-mail`,
  `check-all`, `migrate`).
  - _Req: R16.1, R17.1_
- [ ] 0.3 `config.py` (Settings con Pydantic Settings + carga de `config.yaml`),
  `logging.py` (structlog), `interfaces.py` (los 7 Protocols), `models.py`
  (modelos de dominio base).
  - _Req: R16.3, R16.4_
- [ ] 0.4 `config.yaml` con reglas de prefiltro, pesos de scoring por defecto,
  feature flags de fuentes y periodos. `.env.example` con todas las variables.
  - _Req: R4.1, R5.5, R16.5_
- [ ] 0.5 `profile.example.yaml` con datos ficticios siguiendo el esquema completo.
  - _Req: R1.6, R2 (estructura)_
- [ ] 0.6 Scripts `scripts/check_*.py` para Groq, Neon, Telegram, Drive y correo
  (Graph/Gmail); cada uno confirma conexión y falla con mensaje claro. Subcomando
  `check-all` los ejecuta.
  - _Req: R17.2_
- [ ] 0.7 Tests: carga de config/env, validación de `profile.example.yaml`,
  smoke de CLI.
  - _Req: R16.6_

**Terminado**: cada script `check_*` confirma conexión contra su servicio.
**Configuración manual tuya**: crear cuentas y obtener credenciales (Groq API key;
Neon `DATABASE_URL`; bot de Telegram + `TELEGRAM_OWNER_CHAT_ID`; cuenta de servicio
de Google + `DRIVE_FOLDER_ID`; app registrada para Graph o Gmail). Copiar
`.env.example` a `.env` y rellenar.

---

## Fase 1 — Perfil

- [ ] 1.1 `profile/model.py`: `ProfileModel` (Pydantic v2) con todas las secciones
  del esquema y validadores (ids únicos, `translation_status`).
  - _Req: R1.1, R1.6_
- [ ] 1.2 `profile/validation.py`: existencia de ids, idioma aprobado, tecnología
  presente en perfil; helpers reutilizables por documentos y formularios.
  - _Req: R1.3, R1.4, R1.5, R7.3_
- [ ] 1.3 `profile/importer.py`: extracción de texto de PDF/DOCX; prompt `import_cv`
  con reglas anti-invención; genera `profile.draft.yaml` con ids estables y
  traducción marcada `pending_review`; reporte de campos vacíos/dudosos.
  - _Req: R2.1, R2.2, R2.3, R2.4, R2.5_
- [ ] 1.4 `cli import-cv ruta`: integra extracción + importer + reporte; nunca
  promueve el borrador.
  - _Req: R2.1, R2.6_
- [ ] 1.5 Tests: validación de perfil ok/ko, id inexistente, idioma no aprobado,
  importer con LLM mockeado sobre texto fijo (no inventa campos ausentes).
  - _Req: R16.6_

**Terminado**: importas tu CV real y obtienes `profile.draft.yaml` revisable con
reporte de campos dudosos.
**Configuración manual tuya**: revisar y corregir el borrador; renombrarlo a
`profile.yaml`.

---

## Fase 2 — Recolección

- [ ] 2.1 `storage/migrate.py` + `migrations/001_init.sql`: tablas `jobs`,
  `applications`, `telegram_state`, `schema_migrations` con constraints de
  idempotencia. Subcomando `migrate`.
  - _Req: R12.1, R12.2, R12.3, R12.4, R12.5_
- [ ] 2.2 `storage/neon.py`: `JobRepository` con psycopg 3, retry de conexión,
  `upsert_job` idempotente, estados, `get/set_state`.
  - _Req: R12.1, R3.6, R3.7, R16.7_
- [ ] 2.3 `fetchers/base.py` (ABC + registro) y normalización a `RawJob`.
  - _Req: R3.1_
- [ ] 2.4 Fetchers iniciales: `getonboard.py`, `remoteok.py`, `remotive.py`,
  `weworkremotely.py` (RSS). Errores aislados por fuente.
  - _Req: R3.2, R3.4_
- [ ] 2.5 Fetchers opcionales con flag: `himalayas.py`, `adzuna.py`,
  `gmail_alerts.py` (sin scraping de LinkedIn/Indeed).
  - _Req: R3.3, R3.5_
- [ ] 2.6 `filters/prefilter.py`: reglas deterministas desde `config.yaml`;
  marca `filtered_out`.
  - _Req: R4.1, R4.2_
- [ ] 2.7 Integración parcial del pipeline: collect → dedupe → persist → prefilter.
  - _Req: R3.6, R3.7_
- [ ] 2.8 Tests: dedupe por `(source, external_id)` y url, idempotencia de dos
  corridas, aislamiento de fallo de un fetcher, prefiltro con casos límite.
  - _Req: R16.6, R16.7_

**Terminado**: una ejecución llena `jobs` sin duplicados.
**Configuración manual tuya**: `DATABASE_URL` válido; (opcional) claves Adzuna y
acceso Gmail para la fuente de alertas.

---

## Fase 3 — Análisis y scoring

- [ ] 3.1 `llm/client.py`: `GroqClient`, `OllamaClient`, `FallbackLlmClient` con
  `complete_json` (validación + un reintento) y fallback automático.
  - _Req: R5.1, R5.2_
- [ ] 3.2 `llm/schemas.py` y `llm/prompts.py`: esquemas y prompts con reglas
  anti-invención para `analyze_job`, `score_job`, `confirm_language`.
  - _Req: R5.3, R5.4, R5.7_
- [ ] 3.3 `language.py`: detección con lingua + confirmación LLM en casos dudosos.
  - _Req: R6.1_
- [ ] 3.4 `scoring/scorer.py`: análisis + scoring ponderado configurable + umbral
  `MIN_SCORE`.
  - _Req: R5.3, R5.4, R5.5, R5.6_
- [ ] 3.5 Fixtures de ofertas en es y en; respuestas LLM de ejemplo.
  - _Req: R16.6_
- [ ] 3.6 Tests: parseo/validación de salidas, reintento ante JSON inválido,
  fallback Groq→Ollama (mock), ponderación y umbral, detección de idioma es/en.
  - _Req: R16.6_

**Terminado**: salidas válidas y puntajes coherentes sobre fixtures es/en.
**Configuración manual tuya**: `GROQ_API_KEY`, `GROQ_MODEL`; Ollama local
(`OLLAMA_BASE_URL`, `OLLAMA_MODEL`) si quieres probar el fallback.

---

## Fase 4 — Documentos

- [ ] 4.1 `documents/selector.py`: prompt `select_content` + validación por código
  de ids y de idioma aprobado.
  - _Req: R7.1, R7.2, R7.3_
- [ ] 4.2 Plantillas `templates/cv_{es,en}.html`, `cover_{es,en}.html`, `styles.css`
  (una columna, sin tablas/imágenes/íconos, encabezados localizados).
  - _Req: R8.2, R8.3_
- [ ] 4.3 `documents/render.py`: HTML→PDF con WeasyPrint; carta ≤ 200 palabras;
  nombre de archivo `Nombre_Apellido_Empresa_Cargo_{ES|EN}.pdf`.
  - _Req: R8.1, R8.4, R6.4_
- [ ] 4.4 `storage/drive.py`: `FileStorage` con Google Drive; guarda `web_view_link`.
  - _Req: R9.1, R9.2, R9.3_
- [ ] 4.5 Tests: selección con id inexistente (falla), límite de 200 palabras,
  prueba ATS de orden de lectura del PDF en es y en, upload mockeado.
  - _Req: R8.5, R16.6_

**Terminado**: CV legible por parser ATS en ambos idiomas y enlace guardado.
**Configuración manual tuya**: WeasyPrint deps de sistema (pango/cairo) según OS;
cuenta de servicio de Drive y `DRIVE_FOLDER_ID`.

---

## Fase 5 — Telegram

- [ ] 5.1 `channels/telegram.py`: `Notifier` (enviar propuesta con adjuntos y
  botones), `get_updates` con `last_update_id` en `telegram_state`, solo owner.
  - _Req: R10.1, R10.2, R10.3, R14.2_
- [ ] 5.2 Botones Enviar/Ajustar/Descartar/Ya apliqué/Español/Inglés y comandos
  `/pendientes`, `/estado`, `/entrevista id`, `/rechazo id`, `/buscar`.
  - _Req: R10.3, R10.4, R10.5, R10.6_
- [ ] 5.3 Flujo de idioma dudoso: pregunta por Telegram y espera; regeneración por
  "Ajustar".
  - _Req: R6.3, R10.4_
- [ ] 5.4 `pipeline/run.py`: orquestación completa hasta `proposed` +
  `process_telegram_updates`; guards de `DAILY_APPLICATION_LIMIT` y no repetir
  empresa+cargo.
  - _Req: R10.7, R10.8, R10.9, R14.2_
- [ ] 5.5 Tests: procesamiento idempotente por `update_id`, ignorar chats no owner,
  guards de límite diario y empresa+cargo, transición de estados.
  - _Req: R16.6, R16.7_

**Terminado**: flujo completo hasta aprobación y registro.
**Configuración manual tuya**: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_OWNER_CHAT_ID`.

---

## Fase 6 — Envío

- [ ] 6.1 `mail/base.py` (`Mailer` ABC) y selección por `MAIL_PROVIDER`.
  - _Req: R11.2_
- [ ] 6.2 `mail/graph.py`: Outlook/Hotmail vía Graph (`sendMail`, `Mail.Send`,
  `saveToSentItems`), OAuth con msal; `cli auth-mail` (device code) guarda refresh
  token.
  - _Req: R11.3, R11.5_
- [ ] 6.3 `mail/gmail.py`: Gmail API con CV adjunto en PDF.
  - _Req: R11.4_
- [ ] 6.4 Flujo de envío post-aprobación: correo si hay contacto; modo manual
  (enlace + documentos + botón "Ya apliqué") para otros portales; registro en
  `applications` con fecha/hora, canal, idioma, variante, URLs.
  - _Req: R11.1, R11.6, R11.7, R12.6, R16.7_
- [ ] 6.5 Tests: Mailer mockeado (Graph y Gmail), selección por provider, modo
  manual, registro correcto, no doble envío.
  - _Req: R16.6, R16.7_

**Terminado**: una postulación de prueba registrada de extremo a extremo.
**Configuración manual tuya**: registrar app (Azure AD para Graph o Google Cloud
para Gmail), ejecutar `jobpilot auth-mail`, guardar el refresh token como secreto.

---

## Fase 7 — Seguimiento

- [ ] 7.1 `pipeline/follow_up.py`: recordatorio a los 7 días (configurable) de
  postulaciones sin resultado.
  - _Req: R13.1_
- [ ] 7.2 Comandos de cambio de resultado (`/entrevista id`, `/rechazo id`) y
  reporte semanal por Telegram.
  - _Req: R13.2, R13.3_
- [ ] 7.3 Tests: cálculo de `follow_up_at`, transición de `outcome`, generación de
  reporte semanal.
  - _Req: R16.6_

**Terminado**: recordatorios, comandos de estado y reporte semanal funcionando.

---

## Fase 8 — Formularios asistidos (opcional, local)

- [ ] 8.1 `forms/base.py`: `FormFiller` ABC + detección de plataforma por URL y DOM.
  - _Req: R15.1_
- [ ] 8.2 `greenhouse.py` y `lever.py` con Playwright (navegador visible
  configurable); llenan campos desde perfil, suben CV+carta en el idioma correcto.
  - _Req: R15.2, R15.3_
- [ ] 8.3 Respuestas abiertas con LLM (solo perfil+oferta); preguntas sensibles solo
  desde `answers_bank`; sin respuesta → campo vacío + aviso; marcar dudosos.
  - _Req: R15.3, R15.5_
- [ ] 8.4 Confirmación por Telegram: captura + lista de dudosos con botones
  Enviar/Cancelar; solo con Enviar, clic final y registro.
  - _Req: R15.4_
- [ ] 8.5 Seguridad: CAPTCHA/anti-bot → detener y avisar; estructura inesperada →
  abortar seguro + captura + aviso; nunca enviar incompleto; nunca Easy Apply/Indeed.
  - _Req: R15.6, R15.7, R15.8_
- [ ] 8.6 Tests: detección de plataforma, mapeo de campos, bloqueo de preguntas
  sensibles al LLM, aborto seguro ante DOM inesperado (Playwright mockeado).
  - _Req: R16.6_

**Terminado**: un formulario real de prueba llenado hasta el paso previo al envío,
con captura enviada por Telegram.
**Configuración manual tuya**: `playwright install` en tu PC; ejecutar esta fase
localmente, nunca en CI.

---

## Fase 9 — Documentación y despliegue

- [ ] 9.1 `.github/workflows/pipeline.yml`: cron cada 2h (configurable) +
  `workflow_dispatch`; instala deps de sistema (WeasyPrint) y usa secretos.
  - _Req: R14.1, R16.5_
- [ ] 9.2 `docs/architecture.md` (con diagrama), `docs/decisions.md`,
  `docs/deployment.md` (secretos necesarios y migración a long polling en VM).
  - _Req: R14.3_
- [ ] 9.3 `README.md`: visión general, setup, comandos, lista de configuración
  manual por fase.
  - _Req: R16 (overview)_

**Terminado**: documentación completa y guía de despliegue en GitHub Actions.
