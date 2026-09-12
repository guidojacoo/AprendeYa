# Desplegar fantasybot en $0

De principio a fin: unos 25 minutos. Todo gratis, sin VPS, sin proceso
permanente, sin dejar tu PC encendida.

```
GitHub Actions  ──HTTP──>  Vercel Function  ──>  LaLiga Fantasy API
   (el reloj)                 (el bot)                (el juego)
                                  │
                                  └──> Supabase PostgreSQL (la memoria)
```

Nada vive entre una invocación y la siguiente salvo lo que está en Supabase.

---

## 0. Lo que necesitas antes de empezar

| Cosa | Dónde | Coste |
|---|---|---|
| Cuenta GitHub | github.com | gratis |
| Cuenta Vercel | vercel.com (login con GitHub) | Hobby, gratis |
| Cuenta Supabase | supabase.com | Free, gratis |
| Cuenta LaLiga Fantasy | ya la tienes | gratis |
| Python 3.11+ local | para el login inicial | — |

**Solo hay una cosa que tienes que hacer tú obligatoriamente en tu máquina: el
login de LaLiga.** Es OAuth con navegador, no hay forma de automatizarlo desde
un servidor. Se hace una vez y dura 90 días.

---

## 1. Supabase — la memoria

1. supabase.com → **New project**. Elige región `eu-west` (Irlanda/Londres)
   para estar cerca de LaLiga. Guarda la contraseña de la base aunque no la
   vayas a usar.
2. Espera a que termine de provisionar (~2 min).
3. **SQL Editor → New query**. Pega el contenido de
   `supabase/migrations/0001_init.sql` y dale a **Run**.
   (o: `python scripts/setup-db.py --print` y copias la salida)
4. **Project Settings → Data API**. Copia tres valores:

   | Campo en el dashboard | Variable |
   |---|---|
   | Project URL | `SUPABASE_URL` |
   | `anon` `public` | `SUPABASE_ANON_KEY` (no la usa el bot, pero no las confundas) |
   | `service_role` `secret` | `SUPABASE_SERVICE_ROLE_KEY` ← **esta es la que usa el bot** |

> ⚠️ La `service_role` key se salta Row Level Security. Va en Vercel (server-side)
> y en tu `.env` local. **Nunca** en el navegador, nunca en el repo. Quien la
> tenga es dueño de tu base de datos. Si se filtra, se rota en esa misma página.

5. Verifica:
   ```bash
   export SUPABASE_URL=https://xxxx.supabase.co
   export SUPABASE_SERVICE_ROLE_KEY=eyJ...
   python scripts/setup-db.py
   ```
   Tienen que salir las 9 tablas en `[ok]`.

---

## 2. LaLiga — el login (lo único que solo puedes hacer tú)

En tu máquina, dentro de `fantasybot-serverless/`:

```bash
python -m fantasybot login
```

Imprime una URL. Ábrela, entra con tu cuenta de Google.

**Truco importante:** hazlo en una ventana de incógnito. Con varias cuentas de
Google acumuladas, el selector de cuenta se cuelga y para cuando responde el
código ya ha caducado. Es el fallo nº1 de este paso.

En DevTools → Network (con *Preserve log* activado), busca la petición que
aparece como **(canceled)** y copia su URL `authredirect://...`. Entonces:

```bash
python -m fantasybot login "authredirect://com.lfp.laligafantasy?code=..."
```

Eso escribe `tokens.json`. Compruébalo:

```bash
python -m fantasybot me
python -m fantasybot leagues     # apunta el id si juegas varias ligas
```

Ahora sube esa sesión a Supabase:

```bash
python scripts/migrate-state.py --dry-run --tokens   # mira qué va a subir
python scripts/migrate-state.py --tokens             # súbelo
```

Esto **no borra nada** de `.state/`. Quédatelo como respaldo hasta que el bot
lleve unos días funcionando.

> **Alternativa** si prefieres no subir el fichero: copia solo el refresh token
> con `python -c "import json;print(json.load(open('tokens.json'))['refresh_token'])"`
> y ponlo en la variable `FANTASY_REFRESH_TOKEN` en Vercel. El bot arranca con
> él y escribe el juego completo de tokens en la primera renovación.

---

## 3. Vercel — el bot

1. Genera el secreto compartido:
   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```
2. vercel.com → **Add New → Project** → importa este repositorio.
3. **Root Directory**: `fantasybot-serverless`
   *(este proyecto vive en un subdirectorio; si lo mueves a su propio repo,
   deja Root Directory vacío)*
4. Framework Preset: **Other**. Build Command: vacío. Output Directory: vacío.
5. **Environment Variables** (marca *Production*, *Preview* y *Development*):

   ```
   SUPABASE_URL                 https://xxxx.supabase.co
   SUPABASE_SERVICE_ROLE_KEY    eyJ...
   BOT_CRON_SECRET              <el secreto del paso 1>
   CRON_SECRET                  <el mismo secreto>    # activa el cron diario de respaldo
   FANTASYBOT_STORAGE           supabase
   ```

   Opcionales:
   ```
   FANTASY_REFRESH_TOKEN        <solo si NO subiste tokens.json>
   FANTASYBOT_LEAGUE            <id de liga, si juegas varias>
   LLM_PROVIDER                 groq            # deja "none" para $0 puro
   LLM_API_KEY                  gsk_...
   ```

6. **Deploy**. Cuando termine, copia la URL (`https://tu-proyecto.vercel.app`).

7. Verifica:
   ```bash
   curl https://tu-proyecto.vercel.app/api
   # {"ok":true,"service":"fantasybot","storage":"supabase",...}
   ```
   Si `storage` dice `local`, las variables de Supabase no llegaron.

8. Primer tick a mano:
   ```bash
   curl -X POST "https://tu-proyecto.vercel.app/api/tick?force=1" \
        -H "Authorization: Bearer <BOT_CRON_SECRET>"
   ```

---

## 4. GitHub Actions — el reloj

En el repo: **Settings → Secrets and variables → Actions**

* pestaña **Variables** → New: `VERCEL_APP_URL` = `https://tu-proyecto.vercel.app`
* pestaña **Secrets** → New: `BOT_CRON_SECRET` = el mismo secreto

Luego **Actions** → `fantasybot tick` → **Run workflow** para probarlo ya.
A partir de ahí corre solo cada 5 minutos.

> ⚠️ **Lee la sección de límites más abajo antes de dejarlo en `*/5`** si tu
> repositorio es privado. En repos públicos los minutos de Actions son gratis e
> ilimitados; en privados `*/5` se come el presupuesto en una semana.

---

## 5. El panel

`https://tu-proyecto.vercel.app/dashboard`

Te pide el `BOT_CRON_SECRET`. Se guarda solo en `sessionStorage` de esa pestaña
y viaja como cabecera `Authorization`; nunca se escribe en el HTML.

Muestra: estado, última ejecución, saldo, alineación, acciones programadas,
plan de mercado, tareas, eventos y errores. Se refresca cada 20s.

---

## 6. Comprobar que el bot juega solo

```bash
# 1. ¿Ha corrido?
curl -s "https://tu-proyecto.vercel.app/api/status" \
     -H "Authorization: Bearer <SECRET>" | jq '.last_execution'

# 2. ¿Tiene cosas en cola?
curl -s "https://tu-proyecto.vercel.app/api/status" \
     -H "Authorization: Bearer <SECRET>" | jq '.pending_actions'

# 3. ¿Qué ha hecho?
curl -s "https://tu-proyecto.vercel.app/api/status" \
     -H "Authorization: Bearer <SECRET>" | jq '.events[:10]'
```

O desde tu máquina apuntando a la misma base:

```bash
export SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... FANTASYBOT_STORAGE=supabase
python -m fantasybot queue       # lo que el cloud va a hacer y cuándo
python -m fantasybot tasks       # tareas pendientes
```

**Señales de que va bien**, por orden de aparición:

1. En `executions` aparece una fila nueva cada ~5 min con `status: done`.
2. Tras la primera revisión, `events` tiene una línea `review`.
3. El día que haya un flip rentable, aparece un `bid-plan` en `events` y una
   acción `bid` en la cola con su `execute_at` unos segundos antes del cierre.
4. Al cierre, un `bid` en `events` con el importe real.
5. `pending_actions` vuelve a vaciarse.

---

## 7. Límites reales del free tier

Esto es lo que de verdad te vas a encontrar. Nada está maquillado.

### GitHub Actions — **el límite que importa**

| | Repo público | Repo privado |
|---|---|---|
| Minutos | ilimitados | 2.000/mes (cuenta Free) |
| Coste de `*/5` | 0 € | **~8.600 min/mes → te pasas en 7 días** |

GitHub factura **cada job redondeado al minuto**, aunque dure 8 segundos. 288
ejecuciones diarias = 288 minutos diarios.

* **Repo público** → deja `*/5`. Es gratis de verdad.
* **Repo privado** → sube el cron a `*/20` (72 min/día ≈ 2.160/mes, aún te pasas)
  o `*/30` (48 min/día ≈ 1.440/mes, entra). Edita la línea `cron:` en
  `.github/workflows/fantasybot-tick.yml`.

**Además — y esto no lo arregla ningún plan:** el cron de GitHub Actions es
*best-effort*. En los runners gratuitos un `*/5` se retrasa habitualmente entre
5 y 20 minutos, y a veces se salta ejecuciones. **No puedes confiar en que el
cron caiga a las 20:44:55.**

Por eso el diseño no se lo pide. Ver más abajo.

También: **GitHub desactiva los workflows programados tras 60 días sin actividad
en el repo.** Te llega un email. Un commit cualquiera lo reactiva. Por eso está
además el cron diario de Vercel como red de seguridad.

### Vercel Hobby

| Recurso | Límite | Consumo real |
|---|---|---|
| Invocaciones | ~1M/mes | 8.640/mes con `*/5` — 1% |
| Tiempo de función | 100 GB-hora/mes | ~4 GB-hora con `*/5` — 4% |
| Duración máxima | **60s** | los ticks paran a los 45-50s a propósito |
| Cron jobs | **1 al día** | se usa como respaldo, no como reloj |
| Uso comercial | prohibido | esto es personal, sin problema |

Holgado. El único límite que te toca es el de **60 segundos**, y es justo el que
obliga a que el sniper sea "aguanta lo que puedas y devuelve el testigo".

### Supabase Free

| Recurso | Límite | Consumo real |
|---|---|---|
| Base de datos | 500 MB | < 10 MB con 30 días de histórico |
| Peticiones API | ilimitadas | ~15 por tick |
| **Pausa por inactividad** | **7 días sin actividad** | el bot la toca cada 5 min → nunca se pausa |
| Proyectos activos | 2 | usas 1 |

El riesgo aquí es la pausa por inactividad, y el propio bot la evita. Si paras el
scheduler más de una semana, el proyecto se suspende y hay que reactivarlo a mano
desde el dashboard.

La tabla `events` es la única que crece sin parar. `supabase/migrations/0001_init.sql`
define `fantasybot_prune()`; ejecútala de vez en cuando desde el SQL Editor:

```sql
select public.fantasybot_prune(30);
```

### LLM

Con `LLM_PROVIDER=none` (el valor por defecto) **no se llama a ningún modelo y
el bot funciona al 100%**: la alineación óptima, el cálculo de flips y el sniping
son matemáticas deterministas, no hacen falta tokens.

Si lo activas, la pasada estratégica corre **una vez al día**, no cada 5 minutos.
Son ~30 llamadas al mes de ~3.000 tokens. Eso entra en el free tier de Groq o
Gemini sin despeinarse.

---

## 8. La pregunta honesta: ¿funciona 24/7 de verdad?

**Sí, y sin tu PC. Pero con una salvedad que conviene entender bien.**

**Lo que sí está garantizado:**
* El bot se despierta solo, indefinidamente, sin que nada tuyo esté encendido.
* El estado sobrevive a que cada función muera a los 45 segundos.
* Las pujas no se duplican, ni aunque el cron dispare dos veces, ni aunque una
  función se muera justo después de pujar.
* Si se cae GitHub Actions, el cron diario de Vercel mantiene la revisión.

**La salvedad — precisión del scheduler gratuito:**

El cron de GitHub **no es puntual**. Se retrasa 5-20 minutos con normalidad.
Si el bot dependiera de que el cron cayera en el segundo exacto del cierre del
mercado, perdería pujas constantemente.

La solución que usa este diseño: **el cron no tiene que ser puntual, solo tiene
que llegar a tiempo.**

1. Un tick cualquiera detecta que hay una puja programada y responde
   `sleep_seconds: 214`.
2. El runner de GitHub Actions **duerme esos 214 segundos**. Un runner es un
   reloj preciso y gratuito.
3. Se despierta y llama a `/api/tick?mode=sniper`.
4. La función de Vercel entra en el bucle de `bidding.snipe()` y **puja en el
   segundo correcto**, exactamente igual que hacía el `bid-run` original.

**Precisión efectiva: ~1-2 segundos sobre el cierre.** La misma que tenías con un
VPS.

**Cuándo puede fallar** (y no lo voy a esconder):

* Si el cron se retrasa **más de 20 minutos** (`MAX_HOLD_SECONDS`) justo antes de
  un cierre, ningún tick entra en la ventana de espera y esa puja no se hace.
  Es raro, pero pasa. Súbelo si quieres más margen — a costa de minutos.
* Si bajas el cron a `*/30` en un repo privado, la ventana se estrecha y el
  riesgo sube bastante. **Repo público + `*/5` es la configuración buena.**
* Si GitHub Actions tiene una caída, se pierde lo que hubiera en esa franja.

**Lo único que sigue necesitando tu intervención:**

El `refresh_token` de LaLiga **caduca a los 90 días** y su OAuth exige navegador.
Cada ~3 meses tienes que repetir el paso 2. No hay forma de evitarlo desde
ningún servidor — es una limitación de LaLiga, no de este despliegue.

---

## 9. Problemas frecuentes

| Síntoma | Causa | Arreglo |
|---|---|---|
| `/api` responde `storage: local` | falta `SUPABASE_SERVICE_ROLE_KEY` en Vercel | añádela y redeploy |
| `/api/tick` → 401 | `BOT_CRON_SECRET` no coincide | tiene que ser idéntico en Vercel y en GitHub Secrets |
| `/api/tick` → `No LaLiga tokens stored` | no migraste la sesión | `python scripts/migrate-state.py --tokens` |
| `Your LaLiga session has expired` | pasaron los 90 días | repite el paso 2 |
| El workflow no arranca solo | GitHub lo desactivó por inactividad | Actions → habilítalo; haz un commit |
| `executions` con `status: failed` | mira `.error` en `/api/status` | suele ser token o liga mal configurada |
| Se pausó Supabase | 7 días sin tocarlo | reactívalo en el dashboard |
| Puja no ejecutada | el cron no entró en la ventana | sube `MAX_HOLD_SECONDS` o baja el intervalo del cron |
| `Supabase storage needs SUPABASE_URL...` en local | el `.env` no está en `fantasybot-serverless/`, o la línea está vacía | el loader ignora `KEY=` sin valor a propósito; compruébalo con `python -c "from fantasybot import config; print(config.SUPABASE_URL)"` |
