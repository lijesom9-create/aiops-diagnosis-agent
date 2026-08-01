/**
 * 会话侧边栏 — 极简风格
 * 管理会话列表、新建/切换/重命名/删除会话
 */

import { useState, useEffect, useRef } from 'react';
import { langgraphApi, type LangGraphSession } from '@/services/api';
import { Plus, MessageSquare, Loader2, Edit2, Trash2, Check, X } from 'lucide-react';

interface SessionSidebarProps {
  currentSessionId: string | null;
  onSessionSelect: (sessionId: string) => void;
  onNewSession: () => void;
  refreshTrigger?: number;
}

export default function SessionSidebar({
  currentSessionId,
  onSessionSelect,
  onNewSession,
  refreshTrigger,
}: SessionSidebarProps) {
  const [sessions, setSessions] = useState<LangGraphSession[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editingTitle, setEditingTitle] = useState('');
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const editInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    loadSessions();
  }, [refreshTrigger]);

  useEffect(() => {
    if (editingId && editInputRef.current) {
      editInputRef.current.focus();
      editInputRef.current.select();
    }
  }, [editingId]);

  const loadSessions = async () => {
    setIsLoading(true);
    try {
      const data = await langgraphApi.listSessions(50);
      setSessions(data.sessions || []);
    } catch (error) {
      console.error('加载会话列表失败:', error);
    } finally {
      setIsLoading(false);
    }
  };

  const handleStartRename = (session: LangGraphSession) => {
    setEditingId(session.session_id);
    setEditingTitle(session.title);
  };

  const handleConfirmRename = async () => {
    if (!editingId || !editingTitle.trim()) {
      setEditingId(null);
      return;
    }
    try {
      await langgraphApi.updateSession(editingId, editingTitle.trim());
      await loadSessions();
    } catch (error) {
      console.error('重命名失败:', error);
    }
    setEditingId(null);
  };

  const handleCancelRename = () => {
    setEditingId(null);
    setEditingTitle('');
  };

  const handleDelete = async (sessionId: string) => {
    try {
      await langgraphApi.deleteSession(sessionId);
      if (sessionId === currentSessionId) {
        onNewSession();
      }
      await loadSessions();
    } catch (error) {
      console.error('删除失败:', error);
    }
    setDeletingId(null);
  };

  const formatDate = (dateStr: string) => {
    try {
      const date = new Date(dateStr);
      const now = new Date();
      const diffMs = now.getTime() - date.getTime();
      const diffMins = Math.floor(diffMs / 60000);
      const diffHours = Math.floor(diffMs / 3600000);
      const diffDays = Math.floor(diffMs / 86400000);

      if (diffMins < 1) return '刚刚';
      if (diffMins < 60) return `${diffMins}分钟前`;
      if (diffHours < 24) return `${diffHours}小时前`;
      if (diffDays < 7) return `${diffDays}天前`;
      return date.toLocaleDateString('zh-CN');
    } catch {
      return '';
    }
  };

  return (
    <div className="flex flex-col h-full">
      {/* 新建按钮 */}
      <button
        onClick={onNewSession}
        className="btn-secondary w-full mb-3"
      >
        <Plus size={15} />
        <span>新建对话</span>
      </button>

      {/* 会话列表 */}
      <div className="flex-1 overflow-y-auto space-y-0.5 -mx-1 px-1">
        {isLoading ? (
          <div className="flex items-center justify-center py-8">
            <Loader2 size={18} className="animate-spin text-zinc-400" />
          </div>
        ) : sessions.length === 0 ? (
          <div className="text-center text-zinc-400 text-[13px] py-8">
            暂无会话
          </div>
        ) : (
          sessions.map((session) => {
            const isEditing = editingId === session.session_id;
            const isDeleting = deletingId === session.session_id;
            const isActive = currentSessionId === session.session_id;

            return (
              <div
                key={session.session_id}
                className={`group relative rounded-lg transition-colors ${
                  isActive
                    ? 'bg-zinc-100'
                    : 'hover:bg-zinc-50'
                }`}
              >
                {isEditing ? (
                  <div className="flex items-center p-2 gap-1">
                    <input
                      ref={editInputRef}
                      value={editingTitle}
                      onChange={e => setEditingTitle(e.target.value)}
                      onKeyDown={e => {
                        if (e.key === 'Enter') handleConfirmRename();
                        if (e.key === 'Escape') handleCancelRename();
                      }}
                      className="input text-[13px] py-1"
                    />
                    <button
                      onClick={handleConfirmRename}
                      className="p-1 text-zinc-600 hover:bg-zinc-200 rounded"
                    >
                      <Check size={15} />
                    </button>
                    <button
                      onClick={handleCancelRename}
                      className="p-1 text-zinc-400 hover:bg-zinc-200 rounded"
                    >
                      <X size={15} />
                    </button>
                  </div>
                ) : isDeleting ? (
                  <div className="p-2.5">
                    <p className="text-[12px] text-red-600 mb-2">确认删除此会话？</p>
                    <div className="flex gap-2">
                      <button
                        onClick={() => handleDelete(session.session_id)}
                        className="btn-danger flex-1 text-[12px] py-1"
                      >
                        删除
                      </button>
                      <button
                        onClick={() => setDeletingId(null)}
                        className="btn-secondary flex-1 text-[12px] py-1"
                      >
                        取消
                      </button>
                    </div>
                  </div>
                ) : (
                  <button
                    onClick={() => onSessionSelect(session.session_id)}
                    className="w-full text-left p-2.5"
                  >
                    <div className="flex items-start gap-2">
                      <MessageSquare
                        size={14}
                        className={`mt-0.5 flex-shrink-0 ${
                          isActive ? 'text-zinc-700' : 'text-zinc-400 group-hover:text-zinc-600'
                        }`}
                      />
                      <div className="flex-1 min-w-0">
                        <div
                          className={`text-[13px] truncate ${
                            isActive ? 'text-zinc-900 font-medium' : 'text-zinc-700'
                          }`}
                        >
                          {session.title || '新对话'}
                        </div>
                        <div className="flex items-center gap-1.5 mt-0.5">
                          <span className="text-[11px] text-zinc-400">
                            {formatDate(session.updated_at)}
                          </span>
                          {session.message_count > 0 && (
                            <span className="text-[11px] text-zinc-400">
                              · {session.message_count} 条
                            </span>
                          )}
                        </div>
                      </div>
                    </div>
                  </button>
                )}

                {/* 悬浮操作按钮 */}
                {!isEditing && !isDeleting && (
                  <div className="absolute right-1.5 top-1.5 flex gap-0.5 opacity-0 group-hover:opacity-100 transition-opacity">
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        handleStartRename(session);
                      }}
                      className="p-1 text-zinc-400 hover:text-zinc-900 hover:bg-zinc-200 rounded"
                      title="重命名"
                    >
                      <Edit2 size={13} />
                    </button>
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        setDeletingId(session.session_id);
                      }}
                      className="p-1 text-zinc-400 hover:text-red-600 hover:bg-red-50 rounded"
                      title="删除"
                    >
                      <Trash2 size={13} />
                    </button>
                  </div>
                )}
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}
