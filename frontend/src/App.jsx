import { NavLink, Route, Routes } from 'react-router-dom'
import ImagesPage from './pages/ImagesPage'
import ImageDetailPage from './pages/ImageDetailPage'
import AnnotatePage from './pages/AnnotatePage'
import ReviewPage from './pages/ReviewPage'
import ExportsPage from './pages/ExportsPage'

export default function App() {
  return (
    <div>
      <nav className="topnav">
        <strong>分割标注工作台</strong>
        <NavLink to="/">图片</NavLink>
        <NavLink to="/exports">训练集导出</NavLink>
      </nav>
      <main>
        <Routes>
          <Route path="/" element={<ImagesPage />} />
          <Route path="/images/:id" element={<ImageDetailPage />} />
          <Route path="/annotate/:id" element={<AnnotatePage />} />
          <Route path="/review/:id" element={<ReviewPage />} />
          <Route path="/exports" element={<ExportsPage />} />
        </Routes>
      </main>
    </div>
  )
}
