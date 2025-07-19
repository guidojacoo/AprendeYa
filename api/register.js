const express = require('express')
const bcrypt = require('bcrypt')
const db = require('./db')
const router = express.Router()

router.post('/', async (req, res) => {
  const { email, password } = req.body
  if (!email || !password) return res.status(400).json({ error: 'faltan datos' })

  try {
    const exists = await db.query('SELECT id FROM usuarios WHERE email=$1', [email])
    if (exists.rows.length) return res.status(400).json({ error: 'email ya registrado' })

    const hash = await bcrypt.hash(password, 10)
    await db.query('INSERT INTO usuarios (email, password) VALUES ($1, $2)', [email, hash])

    res.json({ mensaje: 'usuario creado' })
  } catch {
    res.status(500).json({ error: 'error en el servidor' })
  }
})

module.exports = router
