/**
 * 文档管理页 — 上传 / 列表 / 删除 / 重试
 *
 * 调用：
 *   GET    /api/documents/          列表（admin 看全部）
 *   POST   /api/documents/upload    单文件上传
 *   POST   /api/documents/batch-upload  批量上传
 *   DELETE /api/documents/{id}      删除
 *   POST   /api/documents/{id}/retry  重试失败文档
 */

import { useEffect, useState, useCallback, useRef } from 'react';
import { documentsApi } from '@/services/api';
import { toast } from '@/store/toastStore';
import type { DocumentInfo } from '@/types';
import {
  Upload, Trash2, RotateCcw, RefreshCw, Loader2, FileText, Plus, X,
} from 'lucide-react';

// 支持的文件类型
const ACCEPTED = '.pdf,.docx,.txt,.md,.markdown';

// 状态 → 标签样式
const STATUS_TAG: Record<string, { label: string; cls: string }> = {
  completed: { label: '已完成', cls: 'tag-success' },
  processing: { label: '处理中', cls: 'tag-info' },
  pending: { label: '等待中', cls: 'tag-warning' },
  failed: { label: '失败', cls: 'tag-error' },
};

export default function DocumentsPage() {
  const [docs, setDocs] = useState<DocumentInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [uploading, setUploading] = useState(false);

  // 上传弹窗
  const [showUpload, setShowUpload] = useState(false);
  const [selectedFiles, setSelectedFiles] = useState<File[]>([]);
  const [category, setCategory] = useState('other');
  const fileInputRef = useRef<HTMLInputElement>(null);

  // 删除确认
  const [deletingId, setDeletingId] = useState<string | null>(null);
  // 重试中
  const [retryingId, setRetryingId] = useState<string | null>(null);

  const fetchDocs = useCallback(async (isRefresh = false) => {
    if (isRefresh) setRefreshing(true);
    else setLoading(true);
    try {
      const data = await documentsApi.list();
      setDocs(data.documents);
    } catch (err) {
      console.error('获取文档列表失败:', err);
      toast.error('获取文档列表失败');
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    fetchDocs();
  }, [fetchDocs]);

  // 轮询：有待处理的文档时自动刷新
  useEffect(() => {
    const hasPending = docs.some((d) => d.status === 'pending' || d.status === 'processing');
    if (!hasPending) return;
    const timer = setInterval(() => fetchDocs(true), 5000);
    return () => clearInterval(timer);
  }, [docs, fetchDocs]);

  // ========== 上传 ==========

  const handleFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files || []);
    setSelectedFiles((prev) => [...prev, ...files]);
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    const files = Array.from(e.dataTransfer.files);
    setSelectedFiles((prev) => [...prev, ...files]);
  };

  const removeSelectedFile = (idx: number) => {
    setSelectedFiles((prev) => prev.filter((_, i) => i !== idx));
  };

  const handleUpload = async () => {
    if (selectedFiles.length === 0) return;
    setUploading(true);
    try {
      if (selectedFiles.length === 1) {
        await documentsApi.upload(selectedFiles[0], category);
        toast.success(`已上传: ${selectedFiles[0].name}`);
      } else {
        const result = await documentsApi.batchUpload(selectedFiles);
        toast.success(`批量上传完成: 成功 ${result.success}, 跳过 ${result.skipped}, 失败 ${result.failed}`);
      }
      setShowUpload(false);
      setSelectedFiles([]);
      fetchDocs(true);
    } catch (err: any) {
      const detail = err.response?.data?.detail || '上传失败';
      toast.error(typeof detail === 'string' ? detail : '上传失败');
    } finally {
      setUploading(false);
    }
  };

  // ========== 删除 ==========

  const handleDelete = async (docId: string) => {
    setDeletingId(docId);
    try {
      await documentsApi.remove(docId);
      toast.success('文档已删除');
      setDocs((prev) => prev.filter((d) => d.document_id !== docId));
    } catch (err: any) {
      toast.error(err.response?.data?.detail || '删除失败');
    } finally {
      setDeletingId(null);
    }
  };

  // ========== 重试 ==========

  const handleRetry = async (docId: string) => {
    setRetryingId(docId);
    try {
      await documentsApi.retry(docId);
      toast.success('已重新提交处理');
      fetchDocs(true);
    } catch (err: any) {
      toast.error(err.response?.data?.detail || '重试失败');
    } finally {
      setRetryingId(null);
    }
  };

  return (
    <div className="flex-1 overflow-auto">
      {/* 页头 */}
      <header className="h-14 px-6 border-b border-zinc-200 bg-white flex items-center justify-between flex-shrink-0">
        <h1 className="text-sm font-semibold text-zinc-900">文档管理</h1>
        <div className="flex items-center gap-2">
          <button onClick={() => fetchDocs(true)} disabled={refreshing} className="btn-ghost">
            {refreshing ? <Loader2 size={14} className="animate-spin" /> : <RefreshCw size={14} />}
          </button>
          <button onClick={() => setShowUpload(true)} className="btn-primary">
            <Plus size={14} />
            <span>导入文档</span>
          </button>
        </div>
      </header>

      {/* 文档列表 */}
      <div className="p-6">
        {loading ? (
          <div className="flex items-center justify-center py-20">
            <Loader2 size={24} className="animate-spin text-zinc-400" />
          </div>
        ) : docs.length === 0 ? (
          <div className="text-center py-20">
            <FileText size={32} className="mx-auto text-zinc-300 mb-3" />
            <p className="text-sm text-zinc-400">暂无文档</p>
          </div>
        ) : (
          <div className="card overflow-hidden">
            <table className="w-full">
              <thead>
                <tr className="border-b border-zinc-200 bg-zinc-50">
                  <th className="text-left px-4 py-2.5 text-[13px] font-medium text-zinc-500">文件名</th>
                  <th className="text-left px-4 py-2.5 text-[13px] font-medium text-zinc-500">状态</th>
                  <th className="text-right px-4 py-2.5 text-[13px] font-medium text-zinc-500">分块数</th>
                  <th className="text-left px-4 py-2.5 text-[13px] font-medium text-zinc-500">创建时间</th>
                  <th className="text-right px-4 py-2.5 text-[13px] font-medium text-zinc-500">操作</th>
                </tr>
              </thead>
              <tbody>
                {docs.map((doc) => {
                  const st = STATUS_TAG[doc.status] || STATUS_TAG.pending;
                  return (
                    <tr key={doc.document_id} className="border-b border-zinc-100 last:border-0 hover:bg-zinc-50">
                      <td className="px-4 py-3">
                        <div className="flex items-center gap-2">
                          <FileText size={15} className="text-zinc-400 flex-shrink-0" />
                          <div className="min-w-0">
                            <p className="text-[13px] font-medium text-zinc-900 truncate">{doc.title || doc.filename}</p>
                            <p className="text-[11px] text-zinc-400 truncate">{doc.filename}</p>
                          </div>
                        </div>
                      </td>
                      <td className="px-4 py-3">
                        <span className={st.cls}>{st.label}</span>
                      </td>
                      <td className="px-4 py-3 text-right text-[13px] text-zinc-600 tabular-nums">
                        {doc.chunk_count}
                      </td>
                      <td className="px-4 py-3 text-[13px] text-zinc-500">
                        {doc.created_at ? new Date(doc.created_at).toLocaleString('zh-CN') : '-'}
                      </td>
                      <td className="px-4 py-3">
                        <div className="flex items-center justify-end gap-1">
                          {(doc.status === 'failed' || doc.status === 'completed') && (
                            <button
                              onClick={() => handleRetry(doc.document_id)}
                              disabled={retryingId === doc.document_id}
                              className="btn-ghost px-2 py-1"
                              title="重试"
                            >
                              {retryingId === doc.document_id ? (
                                <Loader2 size={13} className="animate-spin" />
                              ) : (
                                <RotateCcw size={13} />
                              )}
                            </button>
                          )}
                          <button
                            onClick={() => handleDelete(doc.document_id)}
                            disabled={deletingId === doc.document_id}
                            className="btn-ghost px-2 py-1 text-red-500 hover:text-red-600 hover:bg-red-50"
                            title="删除"
                          >
                            {deletingId === doc.document_id ? (
                              <Loader2 size={13} className="animate-spin" />
                            ) : (
                              <Trash2 size={13} />
                            )}
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* 上传弹窗 */}
      {showUpload && (
        <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-40" onClick={() => !uploading && setShowUpload(false)}>
          <div
            className="bg-white rounded-xl border border-zinc-200 w-full max-w-lg mx-4 animate-slide-up"
            onClick={(e) => e.stopPropagation()}
          >
            {/* 弹窗头 */}
            <div className="flex items-center justify-between px-5 py-3.5 border-b border-zinc-200">
              <h2 className="text-sm font-semibold text-zinc-900">导入文档</h2>
              <button onClick={() => !uploading && setShowUpload(false)} className="text-zinc-400 hover:text-zinc-600">
                <X size={16} />
              </button>
            </div>

            {/* 弹窗内容 */}
            <div className="p-5 space-y-4">
              {/* 拖拽区 */}
              <div
                onDrop={handleDrop}
                onDragOver={(e) => e.preventDefault()}
                onClick={() => fileInputRef.current?.click()}
                className="border-2 border-dashed border-zinc-200 rounded-lg p-6 text-center cursor-pointer hover:border-zinc-300 transition-colors"
              >
                <Upload size={24} className="mx-auto text-zinc-300 mb-2" />
                <p className="text-[13px] text-zinc-500">点击或拖拽文件到此处</p>
                <p className="text-[11px] text-zinc-400 mt-1">支持 PDF / Word / TXT / Markdown</p>
                <input
                  ref={fileInputRef}
                  type="file"
                  multiple
                  accept={ACCEPTED}
                  onChange={handleFileSelect}
                  className="hidden"
                />
              </div>

              {/* 已选文件列表 */}
              {selectedFiles.length > 0 && (
                <div className="space-y-1.5 max-h-40 overflow-auto">
                  {selectedFiles.map((f, idx) => (
                    <div key={idx} className="flex items-center gap-2 px-3 py-1.5 bg-zinc-50 rounded-lg">
                      <FileText size={13} className="text-zinc-400 flex-shrink-0" />
                      <span className="flex-1 text-[13px] text-zinc-700 truncate">{f.name}</span>
                      <span className="text-[11px] text-zinc-400">{(f.size / 1024).toFixed(1)} KB</span>
                      <button
                        onClick={() => removeSelectedFile(idx)}
                        className="text-zinc-400 hover:text-zinc-600"
                        disabled={uploading}
                      >
                        <X size={13} />
                      </button>
                    </div>
                  ))}
                </div>
              )}

              {/* 分类选择 */}
              <div>
                <label className="block text-[13px] font-medium text-zinc-700 mb-1.5">分类</label>
                <select
                  value={category}
                  onChange={(e) => setCategory(e.target.value)}
                  className="input"
                  disabled={uploading}
                >
                  <option value="other">其他</option>
                  <option value="course">课程</option>
                  <option value="reference">参考资料</option>
                  <option value="faq">常见问题</option>
                  <option value="guide">指南</option>
                </select>
              </div>
            </div>

            {/* 弹窗底部 */}
            <div className="flex items-center justify-end gap-2 px-5 py-3.5 border-t border-zinc-200">
              <button onClick={() => setShowUpload(false)} className="btn-secondary" disabled={uploading}>
                取消
              </button>
              <button onClick={handleUpload} className="btn-primary" disabled={uploading || selectedFiles.length === 0}>
                {uploading ? (
                  <>
                    <Loader2 size={14} className="animate-spin" />
                    <span>上传中</span>
                  </>
                ) : (
                  <span>上传 {selectedFiles.length > 0 ? `(${selectedFiles.length})` : ''}</span>
                )}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
