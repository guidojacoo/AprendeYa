const express = require('express')
const bcrypt = require('bcrypt')
const jwt = require('jsonwebtoken')
const db = require('./db')
const router = express.Router()

router.post('/', async (req, res) => {
  const { email, password } = req.body
  if (!email || !password) return res.status(400).json({ error: 'faltan datos' })

  try {
    const user = await db.query('SELECT * FROM usuarios WHERE email=$1', [email])
    if (!user.rows.length) return res.status(400).json({ error: 'usuario no existe' })

    const valid = await bcrypt.compare(password, user.rows[0].password)
    if (!valid) return res.status(400).json({ error: 'contraseña incorrecta' })

    const token = jwt.sign({ id: user.rows[0].id, email: user.rows[0].email }, process.env.JWT_SECRET, { expiresIn: '1d' })

    res.json({ token })
  } catch {
    res.status(500).json({ error: 'error en el servidor' })
  }
})

module.exports = router
