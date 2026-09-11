# fantasybot serverless — vive en `fantasybot-serverless/`

Esta rama añade una versión serverless de
[jonortega20/fantasybot](https://github.com/jonortega20/fantasybot) que corre
24/7 en free tier: **Vercel** (funciones) + **Supabase** (estado) +
**GitHub Actions** (scheduler). Sin VPS, sin proceso permanente, sin tu PC.

**No toca nada del proyecto AprendeYa.** Va en su propio subdirectorio porque
borrar los ficheros de AprendeYa para hacerle sitio habría sido destructivo e
innecesario: Vercel sabe desplegar desde un subdirectorio.

## Empezar

→ **[`fantasybot-serverless/DEPLOY.md`](fantasybot-serverless/DEPLOY.md)** —
guía completa de despliegue, variables de entorno y límites reales del free tier.

→ **[`fantasybot-serverless/README.md`](fantasybot-serverless/README.md)** —
qué hace el bot y cómo está construido.

## Al desplegar en Vercel

**Root Directory: `fantasybot-serverless`** (Settings → General).

## Si prefieres darle su propio repositorio

Es lo más limpio a medio plazo, y son dos comandos:

```bash
git subtree split --prefix=fantasybot-serverless -b fantasybot-only
git push git@github.com:TU-USUARIO/fantasybot-serverless.git fantasybot-only:main
```

Después, en el repo nuevo: deja Root Directory vacío en Vercel y copia
`.github/workflows/fantasybot-*.yml` a su raíz (los workflows solo se ejecutan
desde `.github/workflows/` en la raíz del repositorio).

## Workflows

En la raíz de este repo, porque GitHub Actions no los lee desde subdirectorios:

- `.github/workflows/fantasybot-tick.yml` — el scheduler (cada 5 min)
- `.github/workflows/fantasybot-ci.yml` — tests, lint y typecheck del subdirectorio
