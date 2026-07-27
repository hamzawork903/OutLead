import { Routes, Route } from 'react-router-dom'
import Sidebar from './components/Sidebar.jsx'
import Dashboard from './pages/Dashboard.jsx'
import Campaigns from './pages/Campaigns.jsx'
import Sender from './pages/Sender.jsx'
import Sequences from './pages/Sequences.jsx'

export default function App() {
  return (
    <div className="min-h-screen">
      <Sidebar />
      <main className="ml-60 px-8 py-8 mx-auto max-w-[1280px]">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/campaigns" element={<Campaigns />} />
          <Route path="/sender" element={<Sender />} />
          <Route path="/sequences" element={<Sequences />} />
        </Routes>
      </main>
    </div>
  )
}
