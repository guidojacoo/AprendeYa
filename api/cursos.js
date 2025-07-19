const express = require('express')
const db = require('./db')
const authMiddleware = require('./auth')
const router = express.Router()

router.get('/', async (req, res) => {
  try {
    const cursos = await db.query('SELECT cursos.*, usuarios.email as creador FROM cursos JOIN usuarios ON cursos.usuario_id = usuarios.id')
    res.json(cursos.rows)
  } catch {
    res.status(500).json({ error: 'error al obtener cursos' })
  }
})

router.post('/', authMiddleware, async (req, res) => {
  const { titulo, nivel, descripcion, link } = req.body
  if (!titulo || !nivel || !descripcion || !link) return res.status(400).json({ error: 'faltan datos' })

  try {
    await db.query(
      'INSERT INTO cursos (titulo, nivel, descripcion, link, usuario_id) VALUES ($1, $2, $3, $4, $5)',
      [titulo, nivel, descripcion, link, req.user.id]
    )
    res.json({ mensaje: 'curso creado' })
  } catch {
    res.status(500).json({ error: 'error al crear curso' })
  }
})

module.exports = router
