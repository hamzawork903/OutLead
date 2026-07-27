import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import App from './App.jsx'
import './index.css'

// Flask serves the SPA under /outreach — the router lives there too.
ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <BrowserRouter basename="/outreach">
      <App />
    </BrowserRouter>
  </React.StrictMode>,
)
