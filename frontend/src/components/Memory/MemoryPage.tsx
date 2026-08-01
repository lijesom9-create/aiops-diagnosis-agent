/**
 * 记忆管理页面
 * 用户画像、对话历史、档案记忆
 */

import { useState, useEffect } from 'react';
import { Brain, User, MessageSquare, BookOpen, Trash2, Loader2 } from 'lucide-react';
import { memoryApi } from '@/services/api';
import { useToastStore } from '@/store/toastStore';

interface UserProfile {
  user_id: string;
  name: string;
  learning_style: string;
  weak_topics: string[];
  strong_topics: string[];
}

interface MemoryStats {
  user_id: string;
  has_profile: boolean;
  weak_topics: number;
  strong_topics: number;
  sessions: number;
  total_turns: number;
  archival_entries: number;
}

export default function MemoryPage() {
  const [profile, setProfile] = useState<UserProfile | null>(null);
  const [stats, setStats] = useState<MemoryStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [newWeakTopic, setNewWeakTopic] = useState('');
  const [newStrongTopic, setNewStrongTopic] = useState('');
  const toast = useToastStore();

  // 加载数据
  const loadData = async () => {
    try {
      setLoading(true);
      const [profileRes, statsRes] = await Promise.all([
        memoryApi.getProfile(),
        memoryApi.getStats(),
      ]);
      setProfile(profileRes);
      setStats(statsRes);
    } catch (error) {
      console.error('加载数据失败:', error);
      toast.error('加载数据失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadData();
  }, []);

  // 添加薄弱知识点
  const handleAddWeakTopic = async () => {
    if (!newWeakTopic.trim()) return;
    try {
      await memoryApi.addWeakTopic(newWeakTopic.trim());
      setNewWeakTopic('');
      await loadData();
      toast.success('已添加薄弱知识点');
    } catch (error) {
      console.error('添加失败:', error);
      toast.error('添加失败，请重试');
    }
  };

  // 添加擅长领域
  const handleAddStrongTopic = async () => {
    if (!newStrongTopic.trim()) return;
    try {
      await memoryApi.addStrongTopic(newStrongTopic.trim());
      setNewStrongTopic('');
      await loadData();
      toast.success('已添加擅长领域');
    } catch (error) {
      console.error('添加失败:', error);
      toast.error('添加失败，请重试');
    }
  };

  // 清空所有记忆
  const handleClearAll = async () => {
    if (!confirm('确定要清空所有记忆吗？此操作不可恢复。')) return;
    try {
      await memoryApi.clearAll();
      await loadData();
      toast.success('已清空所有记忆');
    } catch (error) {
      console.error('清空失败:', error);
      toast.error('清空失败，请重试');
    }
  };

  if (loading) {
    return (
      <div className="h-full flex items-center justify-center">
        <Loader2 className="animate-spin text-zinc-400" size={24} />
      </div>
    );
  }

  return (
    <div className="h-full overflow-y-auto">
      <div className="max-w-4xl mx-auto px-6 py-6 space-y-6">
        {/* 头部 */}
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold text-zinc-900 flex items-center gap-2">
            <Brain size={18} className="text-zinc-900" />
            <span>记忆管理</span>
          </h2>
          <button
            onClick={handleClearAll}
            className="btn-ghost text-red-600 hover:bg-red-50 hover:text-red-700"
          >
            <Trash2 size={14} />
            <span>清空所有</span>
          </button>
        </div>

        {/* 统计卡片 */}
        {stats && (
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            <div className="card p-4">
              <div className="flex items-center gap-1.5 text-zinc-500 mb-2">
                <MessageSquare size={14} />
                <span className="text-[12px]">对话轮次</span>
              </div>
              <p className="text-2xl font-semibold text-zinc-900">{stats.total_turns}</p>
            </div>
            <div className="card p-4">
              <div className="flex items-center gap-1.5 text-zinc-500 mb-2">
                <BookOpen size={14} />
                <span className="text-[12px]">会话数</span>
              </div>
              <p className="text-2xl font-semibold text-zinc-900">{stats.sessions}</p>
            </div>
            <div className="card p-4">
              <div className="flex items-center gap-1.5 text-zinc-500 mb-2">
                <User size={14} />
                <span className="text-[12px]">薄弱知识点</span>
              </div>
              <p className="text-2xl font-semibold text-zinc-900">{stats.weak_topics}</p>
            </div>
            <div className="card p-4">
              <div className="flex items-center gap-1.5 text-zinc-500 mb-2">
                <Brain size={14} />
                <span className="text-[12px]">档案记忆</span>
              </div>
              <p className="text-2xl font-semibold text-zinc-900">{stats.archival_entries}</p>
            </div>
          </div>
        )}

        {/* 用户画像 */}
        <div className="card p-6">
          <h3 className="text-sm font-semibold text-zinc-900 mb-4">用户画像</h3>

          <div className="space-y-5">
            {/* 名称 */}
            <div className="space-y-1">
              <label className="text-[12px] text-zinc-500">名称</label>
              <p className="text-sm text-zinc-900">{profile?.name || '未设置'}</p>
            </div>

            {/* 学习风格 */}
            <div className="space-y-1">
              <label className="text-[12px] text-zinc-500">学习风格</label>
              <p className="text-sm text-zinc-900">{profile?.learning_style || '未设置'}</p>
            </div>

            {/* 薄弱知识点 */}
            <div className="space-y-2">
              <label className="text-[12px] text-zinc-500">薄弱知识点</label>
              <div className="flex flex-wrap gap-1.5">
                {profile?.weak_topics?.map((topic, idx) => (
                  <span key={idx} className="tag-warning">
                    {topic}
                  </span>
                ))}
                {(!profile?.weak_topics || profile.weak_topics.length === 0) && (
                  <span className="text-[13px] text-zinc-400">暂无</span>
                )}
              </div>
              <div className="flex gap-2">
                <input
                  type="text"
                  value={newWeakTopic}
                  onChange={e => setNewWeakTopic(e.target.value)}
                  placeholder="添加薄弱知识点..."
                  className="input flex-1"
                  onKeyDown={e => e.key === 'Enter' && handleAddWeakTopic()}
                />
                <button onClick={handleAddWeakTopic} className="btn-primary">
                  添加
                </button>
              </div>
            </div>

            {/* 擅长领域 */}
            <div className="space-y-2">
              <label className="text-[12px] text-zinc-500">擅长领域</label>
              <div className="flex flex-wrap gap-1.5">
                {profile?.strong_topics?.map((topic, idx) => (
                  <span key={idx} className="tag-success">
                    {topic}
                  </span>
                ))}
                {(!profile?.strong_topics || profile.strong_topics.length === 0) && (
                  <span className="text-[13px] text-zinc-400">暂无</span>
                )}
              </div>
              <div className="flex gap-2">
                <input
                  type="text"
                  value={newStrongTopic}
                  onChange={e => setNewStrongTopic(e.target.value)}
                  placeholder="添加擅长领域..."
                  className="input flex-1"
                  onKeyDown={e => e.key === 'Enter' && handleAddStrongTopic()}
                />
                <button onClick={handleAddStrongTopic} className="btn-primary">
                  添加
                </button>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
