# JobPilot — Requirements

## Introducción

JobPilot es un bot personal en Python que automatiza el ciclo de búsqueda y
postulación a ofertas de trabajo para un único usuario (el dueño). El sistema:

1. Recolecta ofertas desde varias fuentes públicas (APIs y RSS).
2. Las filtra con reglas deterministas baratas antes de usar el LLM.
3. Analiza y puntúa cada oferta contra un perfil único de verdad (`profile.yaml`).
4. Genera un CV en formato ATS y una carta de presentación adaptados a la oferta,
   en el idioma que esta requiera (español o inglés).
5. Propone cada oferta por Telegram con los documentos adjuntos.
6. Tras aprobación explícita del dueño, envía por correo (cuando hay contacto),
   abre modo asistido de formulario (Fase 8), o entrega enlace y documentos para
   aplicación manual.
7. Registra todo en una base de datos Postgres (Neon) y hace seguimiento.

Todos los servicios usan planes gratuitos. El sistema es **human-in-the-loop**:
nada se envía sin aprobación explícita del dueño mediante botones de Telegram.

### Glosario

- **Dueño**: único usuario autorizado, identificado por `TELEGRAM_OWNER_CHAT_ID`.
- **Oferta (job)**: vacante normalizada proveniente de una fuente.
- **Propuesta (proposal)**: mensaje de Telegram con una oferta puntuada y sus
  documentos, a la espera de decisión del dueño.
- **Postulación (application)**: registro de una oferta aprobada y procesada.
- **Perfil**: `profile.yaml`, única fuente de verdad sobre el candidato.
- **Variante de resumen**: uno de `backend_java`, `fullstack`, `cloud`.

---

## Requisitos

### R1. Gestión del perfil como fuente única de verdad

**Historia**: Como dueño, quiero que todo el contenido de mis documentos salga de
un único archivo `profile.yaml`, para que el bot nunca invente hechos sobre mí.

#### Criterios de aceptación

1. CUANDO el sistema arranca ENTONCES DEBE validar `profile.yaml` con un modelo
   Pydantic v2 y fallar con un mensaje claro si falta algún campo requerido.
2. EL sistema DEBE tratar únicamente a `profile.yaml` como fuente de verdad; el
   borrador `profile.draft.yaml` NUNCA se usa en producción.
3. CUANDO el LLM devuelve ids de logros, proyectos o habilidades ENTONCES el
   sistema DEBE verificar por código que cada id existe en el perfil; si alguno no
   existe, DEBE reintentar una vez y, si persiste, marcar la oferta como `error`.
4. EL sistema DEBE usar únicamente textos del perfil marcados como aprobados en el
   idioma requerido; NUNCA traduce hechos al vuelo.
5. SI la oferta pide algo que el perfil no contiene ENTONCES el sistema DEBE
   listarlo como faltante y NUNCA rellenarlo.
6. EL archivo `profile.yaml` DEBE estar en `.gitignore`; el repositorio DEBE
   incluir `profile.example.yaml` con datos ficticios.

### R2. Importación de hoja de vida

**Historia**: Como dueño, quiero importar mi CV en PDF o DOCX a un borrador
estructurado, para no transcribir mi perfil a mano.

#### Criterios de aceptación

1. CUANDO ejecuto `jobpilot import-cv ruta/al/archivo` ENTONCES el sistema DEBE
   extraer el texto del PDF o DOCX.
2. EL sistema DEBE usar el LLM para convertir el texto en `profile.draft.yaml` con
   la estructura definida, asignando un id estable a cada logro y proyecto.
3. SI falta la versión en un idioma (es o en) ENTONCES el sistema DEBE proponer la
   traducción y marcarla con `translation_status: pending_review`.
4. EL sistema DEBE imprimir un reporte de campos vacíos, dudosos o no extraídos.
5. EL LLM NO DEBE añadir información que no esté en el documento; si un dato no
   aparece, DEBE dejarlo vacío.
6. EL borrador DEBE ser revisado y renombrado a `profile.yaml` por el dueño a mano;
   el sistema NUNCA promueve el borrador automáticamente.

### R3. Recolección de ofertas

**Historia**: Como dueño, quiero recolectar ofertas desde varias fuentes, para
tener un flujo constante de vacantes sin revisar portales manualmente.

#### Criterios de aceptación

1. CADA fuente DEBE implementar la interfaz `JobSource` y devolver ofertas
   normalizadas con: source, external_id, url, apply_url, title, company,
   location, remote, salary_text, contact_email, language, description, found_at.
2. EL sistema DEBE incluir las fuentes iniciales: GetOnBoard (API), RemoteOK
   (API JSON), Remotive (API) y We Work Remotely (RSS).
3. EL sistema DEBE soportar fuentes opcionales activables por feature flag:
   Himalayas, Adzuna (API con clave) y lectura de alertas de LinkedIn, Indeed y
   Google Jobs desde Gmail (sin scraping).
4. SI un fetcher falla ENTONCES el sistema DEBE registrar el error y continuar con
   las demás fuentes sin detener el pipeline.
5. EL sistema NO DEBE hacer scraping de LinkedIn ni Indeed, ni automatizar Easy
   Apply ni Indeed.
6. EL sistema DEBE deduplicar ofertas por `(source, external_id)` y por `url`.
7. CUANDO el pipeline se ejecuta dos veces ENTONCES NO DEBE duplicar ofertas en la
   base de datos.

### R4. Prefiltro determinista

**Historia**: Como dueño, quiero descartar ofertas con reglas baratas antes de
gastar llamadas al LLM, para ahorrar cuota.

#### Criterios de aceptación

1. EL sistema DEBE aplicar reglas desde `config.yaml` antes de cualquier llamada al
   LLM: país o remoto, palabras clave obligatorias y excluyentes, idioma,
   modalidad y salario mínimo si está declarado.
2. CUANDO una oferta no cumple el prefiltro ENTONCES el sistema DEBE marcarla como
   `filtered_out` y NO llamar al LLM para ella.

### R5. Análisis y scoring con LLM

**Historia**: Como dueño, quiero que cada oferta sea analizada y puntuada contra mi
perfil, para priorizar dónde aplicar.

#### Criterios de aceptación

1. EL cliente LLM DEBE usar Groq como principal y Ollama local como respaldo, con
   fallback automático si Groq falla o se agota la cuota.
2. TODA salida del LLM DEBE ser JSON validado con Pydantic; si no cumple el
   esquema, el sistema DEBE reintentar una vez.
3. EL análisis DEBE extraer: título normalizado, seniority, stack obligatorio y
   deseable, años pedidos, idioma, modalidad, zona horaria, salario, correo de
   contacto, plataforma de aplicación y banderas rojas.
4. EL scoring DEBE producir un puntaje de 0 a 100 con razones, requisitos cumplidos
   (con el id del logro que los respalda), faltantes y recomendación (aplicar,
   aplicar con reservas, descartar).
5. LA ponderación DEBE ser configurable con pesos por defecto: stack obligatorio
   40, seniority y años 20, modalidad y zona horaria 15, idioma 10, salario y
   condiciones 10, otros 5.
6. SOLO las ofertas con puntaje mayor o igual a `MIN_SCORE` DEBEN continuar al
   siguiente paso.
7. TODOS los prompts DEBEN incluir las reglas anti-invención.

### R6. Detección de idioma

**Historia**: Como dueño, quiero que el CV y la carta salgan en el idioma de la
oferta, para aumentar la tasa de respuesta.

#### Criterios de aceptación

1. EL sistema DEBE detectar el idioma de la oferta con una librería de detección
   más confirmación del LLM en casos dudosos.
2. SI el idioma es español ENTONCES los documentos DEBEN generarse en español; SI
   es inglés ENTONCES en inglés.
3. SI el idioma es otro o hay duda ENTONCES el sistema DEBE preguntar por Telegram
   con botones Español, Inglés y Descartar.
4. EL nombre de archivo del CV DEBE seguir el patrón
   `Nombre_Apellido_Empresa_Cargo_ES.pdf` o `_EN.pdf`.
5. EL idioma elegido DEBE guardarse en la tabla `applications`.

### R7. Selección y adaptación de contenido

**Historia**: Como dueño, quiero que el LLM arme el CV eligiendo qué mostrar, pero
solo con datos reales de mi perfil.

#### Criterios de aceptación

1. EL LLM DEBE elegir la variante de resumen, los ids de logros en orden, los ids
   de proyectos y las habilidades a resaltar.
2. EL LLM SOLO DEBE usar datos del perfil en el idioma de la oferta.
3. EL sistema DEBE validar por código que todo id devuelto existe en el perfil
   antes de renderizar.

### R8. Generación de documentos

**Historia**: Como dueño, quiero un CV en PDF formato ATS y una carta corta, para
postular con documentos listos.

#### Criterios de aceptación

1. EL CV DEBE renderizarse desde plantilla HTML a PDF con WeasyPrint.
2. EL CV DEBE ser una sola columna, sin tablas, imágenes ni íconos, con encabezados
   estándar localizados, fuentes comunes, texto seleccionable, fechas consistentes,
   una página por defecto y máximo dos.
3. LOS encabezados DEBEN localizarse: Resumen/Summary, Experiencia/Experience,
   Educación/Education, Habilidades/Skills, Certificaciones/Certifications,
   Proyectos/Projects.
4. LA carta DEBE tener máximo 200 palabras, sin frases genéricas, en el idioma de
   la oferta.
5. EL sistema DEBE incluir una prueba que extraiga el texto del PDF y verifique el
   orden de lectura, para ambos idiomas.

### R9. Almacenamiento de archivos

**Historia**: Como dueño, quiero que los documentos se guarden en la nube y su
enlace quede en la base de datos.

#### Criterios de aceptación

1. EL sistema DEBE implementar la interfaz `FileStorage`; la implementación inicial
   DEBE ser Google Drive con cuenta de servicio en la carpeta `DRIVE_FOLDER_ID`.
2. EL sistema DEBE guardar el `webViewLink` de cada archivo en la base de datos.
3. LA interfaz `FileStorage` DEBE estar lista para cambiar a Cloudflare R2 sin
   tocar la lógica de negocio.

### R10. Propuesta y aprobación por Telegram

**Historia**: Como dueño, quiero recibir cada oferta por Telegram y decidir con
botones, para mantener el control humano.

#### Criterios de aceptación

1. EL bot DEBE responder únicamente al `TELEGRAM_OWNER_CHAT_ID`.
2. LA propuesta DEBE incluir título, empresa, puntaje, idioma, resumen, faltantes,
   enlace de la oferta y los adjuntos de CV y carta.
3. LA propuesta DEBE ofrecer botones: Enviar, Ajustar, Descartar.
4. CUANDO pulso Ajustar ENTONCES el sistema DEBE aceptar una instrucción en lenguaje
   natural y regenerar los documentos.
5. EL bot DEBE soportar botones Ya apliqué, Español, Inglés según el contexto.
6. EL bot DEBE soportar comandos `/pendientes`, `/estado`, `/entrevista id`,
   `/rechazo id`, `/buscar`.
7. EL sistema DEBE respetar `DAILY_APPLICATION_LIMIT` (por defecto 5) y `MIN_SCORE`
   (por defecto 70).
8. EL sistema NO DEBE proponer dos veces la misma empresa y cargo dentro de un
   periodo configurable.
9. NADA se envía sin que el dueño pulse el botón de aprobación explícito.

### R11. Envío y modos de aplicación

**Historia**: Como dueño, quiero que tras aprobar, el bot gestione el envío según
el caso.

#### Criterios de aceptación

1. SI hay correo de contacto ENTONCES, tras pulsar Enviar, el sistema DEBE enviar
   por correo con el CV adjunto en PDF.
2. EL sistema DEBE implementar la interfaz `Mailer` con dos implementaciones
   seleccionables por `MAIL_PROVIDER` (`outlook` o `gmail`).
3. LA implementación principal DEBE ser Outlook/Hotmail vía Microsoft Graph API
   (`sendMail`, permiso delegado `Mail.Send`, `saveToSentItems` activado), con
   OAuth 2.0 usando msal, consentimiento inicial por script `jobpilot auth-mail` y
   refresh token almacenado como secreto.
4. LA segunda implementación DEBE ser Gmail API.
5. EL sistema NO DEBE usar SMTP con contraseña salvo como opción documentada y
   desactivada por defecto.
6. SI hay formulario de una plataforma soportada ENTONCES el sistema DEBE ofrecer
   el modo asistido de la Fase 8.
7. PARA cualquier otro portal ENTONCES el sistema DEBE entregar enlace y documentos
   y esperar el botón Ya apliqué.

### R12. Registro y modelo de datos

**Historia**: Como dueño, quiero que todo quede registrado, para hacer seguimiento.

#### Criterios de aceptación

1. EL sistema DEBE usar Neon (Postgres) con psycopg 3 y reintentar la conexión
   porque Neon puede estar dormido.
2. EL sistema DEBE gestionar migraciones con SQL versionado o Alembic.
3. LA tabla `jobs` DEBE tener: id, source, external_id, url (único), apply_url,
   title, company, location, remote, salary_text, contact_email, language,
   description, analysis (JSONB), score, score_reason, missing_requirements (JSONB),
   status, found_at; con único `(source, external_id)` y estados: new,
   filtered_out, scored, proposed, approved, applied, discarded, error.
4. LA tabla `applications` DEBE tener: id, job_id (FK), language, cv_variant,
   cv_url, cover_letter_url, channel (email, form_assisted, portal, manual),
   applied_at, follow_up_at, outcome (pending, interview, rejected, offer,
   no_response), notes, created_at.
5. LA tabla `telegram_state` DEBE tener: key, value.
6. EL registro DEBE incluir fecha y hora, canal, idioma, variante de CV y las URLs
   del CV y la carta.

### R13. Seguimiento

**Historia**: Como dueño, quiero recordatorios y reportes, para no perder
oportunidades.

#### Criterios de aceptación

1. EL sistema DEBE enviar un recordatorio a los 7 días (configurable) de una
   postulación sin resultado.
2. EL sistema DEBE ofrecer comandos para cambiar el resultado de una postulación
   (`/entrevista id`, `/rechazo id`).
3. EL sistema DEBE enviar un reporte semanal por Telegram.

### R14. Ejecución del pipeline

**Historia**: Como dueño, quiero que el pipeline corra solo periódicamente, sin
mantener una máquina encendida.

#### Criterios de aceptación

1. EL pipeline DEBE ejecutarse en GitHub Actions con cron cada 2 horas
   (configurable) y por `workflow_dispatch` manual.
2. CADA ejecución DEBE buscar, puntuar, proponer y procesar las respuestas
   pendientes de Telegram con `getUpdates`, guardando el último `update_id` en
   `telegram_state`.
3. LA documentación DEBE explicar cómo migrar a long polling en una VM sin cambiar
   la lógica.
4. LA Fase 8 (formularios) DEBE correr en el PC local, nunca en GitHub Actions.

### R15. Formularios asistidos (Fase 8, opcional)

**Historia**: Como dueño, quiero que el bot llene formularios de Greenhouse y Lever
y me deje dar el último clic.

#### Criterios de aceptación

1. EL sistema DEBE implementar la interfaz `FormFiller` con una implementación por
   plataforma; las iniciales DEBEN ser Greenhouse y Lever, detectadas por URL y DOM.
2. EL módulo DEBE usar Playwright para Python con navegador visible configurable y
   correr solo en el PC local.
3. EL sistema DEBE llenar campos conocidos desde el perfil, subir CV y carta en el
   idioma correcto, responder preguntas abiertas con el LLM usando solo perfil y
   oferta, y marcar campos dudosos.
4. ANTES de enviar, el sistema DEBE mandar por Telegram una captura del formulario y
   la lista de campos dudosos con botones Enviar y Cancelar; solo si el dueño pulsa
   Enviar DEBE hacer el clic final y registrar la postulación.
5. EL sistema NUNCA DEBE responder con el LLM preguntas sensibles (pretensión
   salarial, autorización de trabajo o visa, reubicación, antecedentes o preguntas
   legales, datos demográficos, años de experiencia en una tecnología concreta);
   DEBE responderlas solo desde `answers_bank` y, si no hay respuesta aprobada,
   dejar el campo vacío y avisar.
6. SI aparece un CAPTCHA o bloqueo anti-bot ENTONCES el sistema DEBE detenerse y
   avisar, NUNCA intentar evadirlo.
7. SI la estructura del formulario no coincide con lo esperado ENTONCES el sistema
   DEBE abortar de forma segura, guardar captura y notificar, NUNCA enviar un
   formulario incompleto.
8. EL sistema NO DEBE soportar LinkedIn Easy Apply ni Indeed.

### R16. Calidad, configuración y seguridad

**Historia**: Como dueño, quiero un proyecto mantenible y seguro, para operarlo con
confianza.

#### Criterios de aceptación

1. EL proyecto DEBE usar Python 3.12, tipado estático, Pydantic v2, ruff (lint y
   formato) y pytest.
2. EL código, nombres, comentarios y commits DEBEN estar en inglés; los mensajes del
   bot por Telegram en español; el CV y la carta en el idioma de la oferta.
3. LA arquitectura DEBE basarse en módulos con interfaces (Protocol o ABC):
   `LlmClient`, `JobSource`, `FileStorage`, `Notifier`, `Mailer`, `JobRepository`,
   `FormFiller`.
4. EL sistema DEBE usar logging estructurado y manejar errores por fuente.
5. NINGÚN secreto DEBE estar en el repositorio; todo por variables de entorno con
   `.env.example`.
6. LAS pruebas unitarias DEBEN usar mocks de los servicios externos.
7. EL sistema DEBE ser idempotente: ejecutar el pipeline dos veces no duplica
   ofertas, propuestas ni envíos.

### R17. Preparación y verificación de credenciales (Fase 0)

**Historia**: Como dueño, quiero scripts que verifiquen cada credencial, para saber
que todo está bien configurado antes de empezar.

#### Criterios de aceptación

1. EL repositorio DEBE tener la estructura definida (paquetes, tests, workflows,
   docs), `pyproject.toml`, `config.yaml`, `.env.example` y `profile.example.yaml`.
2. EL sistema DEBE incluir scripts que prueben cada credencial (Groq, Neon,
   Telegram, Drive, correo) y confirmen conexión.

---

## Ambigüedades y decisiones pendientes

Estas se resuelven en `design.md` con una propuesta, pero las marco para tu
confirmación:

1. **Gestión de dependencias**: propongo **uv** (más rápido, lockfile
   reproducible, compatible con `pyproject.toml`). Se mantiene `requirements.txt`
   exportado para GitHub Actions. ¿De acuerdo o prefieres pip puro?
2. **Migraciones**: propongo **SQL versionado** simple (archivos `NNN_*.sql` +
   runner propio) por ser más liviano que Alembic para un proyecto de un solo
   dueño. ¿Prefieres Alembic?
3. **Correo principal vs. envío**: el enunciado dice en un punto "enviar por Gmail"
   (flujo funcional) pero la sección de stack define Outlook como implementación
   principal vía `MAIL_PROVIDER`. **Resuelvo**: el provider es configurable; por
   defecto `outlook`, y el texto de "enviar por correo" aplica al provider activo.
   ¿Confirmas que Outlook es el default?
4. **WeasyPrint** requiere dependencias de sistema (pango, cairo). En GitHub
   Actions se instalan por apt; en local documentaré la instalación por OS. La
   generación de PDF solo ocurre en el paso de documentos. ¿OK?
5. **Groq**: el modelo va en `GROQ_MODEL`. ¿Tienes un modelo preferido ya, o
   propongo un default (p. ej. `llama-3.3-70b-versatile`) en `.env.example`?
6. **Detección de idioma**: propongo **lingua** (más preciso en textos cortos que
   langdetect) con confirmación del LLM en casos dudosos. ¿De acuerdo?
7. **answers_bank sensibles**: años de experiencia en una tecnología concreta se
   trata como pregunta sensible (nunca LLM). Pero el scoring sí calcula años totales
   del perfil. Confirmo que la restricción aplica solo al llenado de formularios, no
   al análisis interno. ¿Correcto?
8. **DEFAULT_LANGUAGE**: se usa cuando la detección y el LLM no deciden y el dueño no
   responde en un tiempo. Propongo que, sin respuesta, la oferta quede en estado
   `proposed` esperando el botón de idioma y nunca se genere documento por defecto
   solo. ¿Prefieres que `DEFAULT_LANGUAGE` fuerce un idioma tras timeout?
