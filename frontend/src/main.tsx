import { createRoot } from 'react-dom/client'

import { App } from './App'
import { createApi, readBootstrapFragment } from './api'
import './styles.css'

const bootstrapToken = readBootstrapFragment()
const root = document.getElementById('root')
if (!root) throw new Error('Application root is unavailable')

createRoot(root).render(<App api={createApi()} bootstrapToken={bootstrapToken} />)
