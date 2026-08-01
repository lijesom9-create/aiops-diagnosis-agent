/**
 * 文档管理页面
 * 上传、列表、删除文档
 */

import { useState, useEffect, useRef } from 'react';
import { FileText, Trash2, Loader2, File, FileUp, X, Check } from 'lucide-react';
import { documentsApiNew, DocumentInfo } from '@/services/api';
import { useToastStore } from '@/store/toastStore';

// 文档分类选项
const CATEGORY_OPTIONS = [
  { value: 'api_doc', label: 'API 文档' },
  { value: 'ops_sop', label: '运维 SOP' },
  { value: 'architecture', label: '架构设计' },
  { value: 'dev_guide', label: '开发指南' },
  { value: 'troubleshooting', label: '故障排查' },
  { value: 'other', label: '其他' },
];

// 筛选标签（含"全部"）
const FILTER_TABS = [
  { value: '', label: '全部' },
  ...CATEGORY_OPTIONS,
];

// 分类值 -> 中文标签
const getCategoryLabel = (value: string) => {
  return CATEGORY_OPTIONS.find(o => o.value === value)?.label || value;
};

// 文件类型标签（从扩展名派生）
const getFileType = (filename: string) => {
  const ext = filename.split('.').pop()?.toLowerCase() || '';
  return ext || 'file';
};

// 字符数格式化
const formatChars = (n: number) => {
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(n);
};

// 时间格式化
const formatDate = (dateStr: string) => {
  if (!dateStr) return '';
  const d = new Date(dateStr);
  if (isNaN(d.getTime())) return dateStr;
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
};

export default function DocumentsPage() {
  const [documents, setDocuments] = useState<DocumentInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [uploading, setUploading] = useState(false);
  const [confirmingId, setConfirmingId] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [selectedCategory, setSelectedCategory] = useState<string>('other');
  const [filterCategory, setFilterCategory] = useState<string>('');
  const fileInputRef = useRef<HTMLInputElement>(null);
  const toast = useToastStore();

  // 加载文档列表
  const loadDocuments = async (category?: string) => {
    try {
      setLoading(true);
      const response = await documentsApiNew.list(category);
      setDocuments(response.documents || []);
    } catch (error) {
      console.error('加载文档失败:', error);
      toast.error('加载文档列表失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadDocuments();
  }, []);

  // 上传文档
  const handleUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;

    setUploading(true);
    try {
      await documentsApiNew.upload(file, selectedCategory);
      await loadDocuments(filterCategory || undefined);
      toast.success('文档上传成功，正在处理中');
    } catch (error) {
      console.error('上传失败:', error);
      toast.error('上传失败，请重试');
    } finally {
      setUploading(false);
      if (fileInputRef.current) {
        fileInputRef.current.value = '';
      }
    }
  };

  // 切换分类筛选
  const handleFilterChange = (category: string) => {
    setFilterCategory(category);
    loadDocuments(category || undefined);
  };

  // 删除文档
  const handleDelete = async (documentId: string) => {
    setDeletingId(documentId);
    try {
      await documentsApiNew.delete(documentId);
      await loadDocuments(filterCategory || undefined);
      toast.success('文档已删除');
    } catch (error) {
      console.error('删除失败:', error);
      toast.error('删除失败，请重试');
    } finally {
      setDeletingId(null);
      setConfirmingId(null);
    }
  };

  // 状态标签样式
  const getStatusClass = (status: string) => {
    switch (status) {
      case 'completed':
        return 'tag-success';
      case 'processing':
        return 'tag-warning';
      case 'failed':
        return 'tag-error';
      default:
        return 'tag-default';
    }
  };

  // 状态文本
  const getStatusText = (status: string) => {
    switch (status) {
      case 'completed':
        return '已完成';
      case 'processing':
        return '处理中';
      case 'failed':
        return '失败';
      default:
        return status;
    }
  };

  return (
    <div className="h-full overflow-y-auto">
      <div className="max-w-5xl mx-auto px-6 py-6">
        {/* 头部 */}
        <div className="flex items-center justify-between mb-6">
          <div>
            <h2 className="text-lg font-semibold text-zinc-900">文档管理</h2>
            <p className="text-[13px] text-zinc-500 mt-0.5">上传、管理你的学习文档</p>
          </div>
          <div className="flex items-center gap-2">
            <select
              value={selectedCategory}
              onChange={(e) => setSelectedCategory(e.target.value)}
              className="input"
              aria-label="选择上传分类"
            >
              {CATEGORY_OPTIONS.map(opt => (
                <option key={opt.value} value={opt.value}>{opt.label}</option>
              ))}
            </select>
            <button
              onClick={() => fileInputRef.current?.click()}
              disabled={uploading}
              className="btn-primary"
            >
              {uploading ? (
                <Loader2 size={15} className="animate-spin" />
              ) : (
                <FileUp size={15} />
              )}
              <span>{uploading ? '上传中...' : '上传文档'}</span>
            </button>
            <input
              ref={fileInputRef}
              type="file"
              accept=".pdf,.docx,.txt,.md"
              onChange={handleUpload}
              aria-label="选择文件上传"
              className="hidden"
            />
          </div>
        </div>

        {/* 分类筛选标签栏 */}
        <div className="flex items-center gap-1.5 mb-4 flex-wrap">
          {FILTER_TABS.map(tab => (
            <button
              key={tab.value}
              onClick={() => handleFilterChange(tab.value)}
              className={`px-3 py-1 rounded-md text-[13px] ${
                filterCategory === tab.value
                  ? 'bg-zinc-100 text-zinc-900'
                  : 'text-zinc-500'
              }`}
            >
              {tab.label}
            </button>
          ))}
        </div>

        {/* 文档列表 */}
        {loading ? (
          <div className="flex items-center justify-center py-20">
            <Loader2 className="animate-spin text-zinc-400" size={24} />
          </div>
        ) : documents.length === 0 ? (
          <div className="card flex flex-col items-center justify-center py-20">
            <FileText size={40} className="text-zinc-300 mb-4" strokeWidth={1.5} />
            <p className="text-sm text-zinc-700 mb-1">还没有文档</p>
            <p className="text-[12px] text-zinc-400">上传 PDF、Word、TXT 或 Markdown 文件开始</p>
          </div>
        ) : (
          <div className="space-y-2">
            {documents.map(doc => (
              <div
                key={doc.document_id}
                className="card px-4 py-3 flex items-center justify-between gap-3 hover:border-zinc-300 transition-colors"
              >
                {/* 左侧：图标 + 文档名 + 元信息 */}
                <div className="flex items-center gap-3 min-w-0 flex-1">
                  <File size={16} className="text-zinc-400 shrink-0" />
                  <div className="min-w-0 flex-1">
                    <p className="text-sm text-zinc-900 truncate">
                      {doc.title || doc.filename}
                    </p>
                    <p className="text-[12px] text-zinc-500 mt-0.5 truncate">
                      {doc.filename}
                      <span className="mx-1.5 text-zinc-300">·</span>
                      {doc.chunk_count} 分块
                      <span className="mx-1.5 text-zinc-300">·</span>
                      {formatChars(doc.char_count)} 字符
                      <span className="mx-1.5 text-zinc-300">·</span>
                      {formatDate(doc.created_at)}
                    </p>
                  </div>
                </div>

                {/* 右侧：分类标签 + 类型标签 + 状态标签 + 操作 */}
                <div className="flex items-center gap-2 shrink-0">
                  <span className="tag-default">{getCategoryLabel(doc.category)}</span>
                  <span className="tag-default uppercase">{getFileType(doc.filename)}</span>
                  <span className={getStatusClass(doc.status)}>{getStatusText(doc.status)}</span>
                  {confirmingId === doc.document_id ? (
                    <div className="flex items-center gap-1">
                      <button
                        onClick={() => handleDelete(doc.document_id)}
                        disabled={deletingId === doc.document_id}
                        className="btn-danger px-2 py-1 text-[12px]"
                        aria-label="确认删除"
                      >
                        {deletingId === doc.document_id ? (
                          <Loader2 size={13} className="animate-spin" />
                        ) : (
                          <Check size={13} />
                        )}
                        <span>确认</span>
                      </button>
                      <button
                        onClick={() => setConfirmingId(null)}
                        disabled={deletingId === doc.document_id}
                        className="btn-ghost px-2 py-1 text-[12px]"
                        aria-label="取消删除"
                      >
                        <X size={13} />
                      </button>
                    </div>
                  ) : (
                    <button
                      onClick={() => setConfirmingId(doc.document_id)}
                      className="btn-ghost p-1.5"
                      aria-label="删除文档"
                    >
                      <Trash2 size={14} />
                    </button>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}

        {/* 底部统计 */}
        {!loading && documents.length > 0 && (
          <p className="text-[12px] text-zinc-400 mt-6">共 {documents.length} 个文档</p>
        )}
      </div>
    </div>
  );
}
