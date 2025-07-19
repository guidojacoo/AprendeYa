require('dotenv').config()
const express = require('express')
const cors = require('cors')

const register = require('./register')
const login = require('./login')
const cursos = require('./cursos')

const app = express()

app.use(cors())
app.use(express.json())

app.use('/api/register', register)
app.use('/api/login', login)
app.use('/api/cursos', cursos)

const PORT = process.env.PORT || 3000

app.listen(PORT, () => {
  console.log(`servidor corriendo en puerto ${PORT}`)
})
