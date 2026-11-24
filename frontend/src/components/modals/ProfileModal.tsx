import React, { useState, useEffect } from 'react';
import { useAppStore } from '../../stores/useAppStore';
import { api } from '../../services/api';
import { Camera, X, Check, User, Mail, Briefcase, Building, Hash, Calendar, Heart } from 'lucide-react';

interface Props {
  onClose: () => void;
}

export const ProfileModal: React.FC<Props> = ({ onClose }) => {
  const { currentUser, showToast } = useAppStore();

  const getInitialName = () => {
    if (currentUser?.display_name && !currentUser.display_name.includes('@')) {
      return currentUser.display_name;
    }
    const raw = currentUser?.display_name || currentUser?.email || currentUser?.username || '';
    if (raw.includes('@')) {
      const local = raw.split('@')[0];
      const parts = local.replace(/[._-]/g, ' ').split(/\s+/).filter(Boolean);
      return parts.map((p) => p.charAt(0).toUpperCase() + p.slice(1)).join(' ');
    }
    return raw;
  };

  const getInitialEmail = () => {
    if (currentUser?.email) return currentUser.email;
    if (currentUser?.username && currentUser.username.includes('@')) return currentUser.username;
    return '';
  };

  const [displayName, setDisplayName] = useState(getInitialName());
  const [email, setEmail] = useState(getInitialEmail());
  const [image, setImage] = useState(currentUser?.image || '');
  const [employeeId, setEmployeeId] = useState(currentUser?.employee_id || '');
  const [department, setDepartment] = useState(currentUser?.department || '');
  const [designation, setDesignation] = useState(currentUser?.designation || '');
  const [dob, setDob] = useState(currentUser?.dob || '');
  const [gender, setGender] = useState(currentUser?.gender || '');

  const [isSaving, setIsSaving] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const initials = (displayName || 'SH')
    .split(/\s+/)
    .map((w) => w[0])
    .join('')
    .slice(0, 2)
    .toUpperCase();

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    if (file.size > 2 * 1024 * 1024) {
      setError('Profile image must be less than 2MB');
      return;
    }
    const reader = new FileReader();
    reader.onload = (ev) => {
      const dataUrl = ev.target?.result as string;
      setImage(dataUrl);
      setError('');
    };
    reader.readAsDataURL(file);
  };

  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!displayName.trim()) {
      setError('Name is required');
      return;
    }
    setIsSaving(true);
    setError('');

    const res = await api.auth.updateProfile({
      display_name: displayName.trim(),
      email: email.trim(),
      image: image.trim(),
      employee_id: employeeId.trim(),
      department: department.trim(),
      designation: designation.trim(),
      dob: dob.trim(),
      gender: gender.trim(),
    });

    setIsSaving(false);

    if (res.ok && res.user) {
      useAppStore.setState({ currentUser: res.user });
      showToast('Profile updated successfully!', 'success');
      onClose();
    } else {
      setError(res.error || 'Failed to update profile');
    }
  };

  return (
    <div className="ca-modal-overlay" style={{ display: 'flex' }} onClick={onClose}>
      <div
        className="profile-modal-box"
        style={{ maxWidth: 600, width: '92%', maxHeight: '90vh', overflowY: 'auto' }}
        onClick={(e) => e.stopPropagation()}
      >
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', borderBottom: '1px solid var(--border-subtle, rgba(255,255,255,0.08))', padding: '18px 24px', background: 'rgba(255, 255, 255, 0.02)' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
            <div style={{ width: 38, height: 38, borderRadius: 10, background: 'linear-gradient(135deg, #3b82f6, #6366f1)', display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#fff', boxShadow: '0 2px 8px rgba(59, 130, 246, 0.35)' }}>
              <User size={20} />
            </div>
            <div>
              <h2 style={{ fontSize: 17, fontWeight: 700, margin: 0, color: 'var(--text, #fff)', letterSpacing: '-0.01em' }}>My Profile</h2>
              <div style={{ fontSize: 12.5, color: 'var(--dim, #94a3b8)', marginTop: 2 }}>Update personal information & profile picture</div>
            </div>
          </div>
          <button
            type="button"
            className="claude-icon-tool"
            onClick={onClose}
            aria-label="Close"
            style={{ width: 30, height: 30, borderRadius: 8, background: 'rgba(255, 255, 255, 0.06)', border: 'none', color: 'var(--text, #fff)', cursor: 'pointer', display: 'flex', alignItems: 'center', justifyContent: 'center' }}
          >
            <X size={16} />
          </button>
        </div>

        <form onSubmit={handleSave} className="flex flex-col gap-4 sm:gap-5 p-4 sm:p-6">
          {/* Avatar Picture Row */}
          <div className="flex flex-col sm:flex-row items-center sm:items-start gap-4 sm:gap-5 p-3.5 sm:p-4 bg-panel2 rounded-xl border border-border text-center sm:text-left">
            <div
              style={{
                width: 72,
                height: 72,
                borderRadius: '50%',
                background: 'linear-gradient(135deg, #6366f1, #8b5cf6)',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                color: '#fff',
                fontWeight: 700,
                fontSize: 24,
                overflow: 'hidden',
                flexShrink: 0,
                border: '2px solid rgba(255, 255, 255, 0.2)',
                boxShadow: '0 4px 16px rgba(0,0,0,0.4)',
              }}
            >
              {image ? (
                <img src={image} alt="Avatar" style={{ width: '100%', height: '100%', objectFit: 'cover' }} />
              ) : (
                <span>{initials}</span>
              )}
            </div>

            <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, justifyContent: 'center' }}>
                <label
                  style={{
                    display: 'inline-flex',
                    alignItems: 'center',
                    gap: 6,
                    padding: '7px 14px',
                    borderRadius: 8,
                    background: 'rgba(255, 255, 255, 0.08)',
                    border: '1px solid rgba(255, 255, 255, 0.16)',
                    fontSize: 12.5,
                    fontWeight: 600,
                    cursor: 'pointer',
                    color: 'var(--text, #fff)',
                    transition: 'background 0.15s ease',
                  }}
                >
                  <Camera size={14} />
                  <span>Upload new photo</span>
                  <input type="file" accept="image/*" onChange={handleFileChange} style={{ display: 'none' }} />
                </label>
                {image && (
                  <button
                    type="button"
                    onClick={() => setImage('')}
                    style={{
                      background: 'transparent',
                      border: 'none',
                      color: 'var(--danger, #f87171)',
                      fontSize: 12,
                      cursor: 'pointer',
                      padding: '4px 8px',
                    }}
                  >
                    Remove
                  </button>
                )}
              </div>
              <span style={{ fontSize: 11.5, color: 'var(--dim, #94a3b8)' }}>Recommended square JPG or PNG, max 2MB</span>
            </div>
          </div>

          {error && (
            <div style={{ padding: '10px 14px', borderRadius: 8, background: 'rgba(239, 68, 68, 0.12)', border: '1px solid rgba(239, 68, 68, 0.3)', color: '#fca5a5', fontSize: 12.5, fontWeight: 500 }}>
              {error}
            </div>
          )}

          {/* Form Grid */}
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3.5 sm:gap-4">
            {/* Display Name */}
            <div>
              <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, fontWeight: 600, color: 'var(--dim, #94a3b8)', marginBottom: 6 }}>
                <User size={13} style={{ color: 'var(--accent, #3b82f6)' }} />
                <span>Full Name *</span>
              </label>
              <input
                type="text"
                className="profile-input"
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
                placeholder="e.g. Sabbir Hossain"
                required
              />
            </div>

            {/* Email */}
            <div>
              <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, fontWeight: 600, color: 'var(--dim, #94a3b8)', marginBottom: 6 }}>
                <Mail size={13} style={{ color: 'var(--accent, #3b82f6)' }} />
                <span>Email Address</span>
              </label>
              <input
                type="email"
                className="profile-input"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="name@company.com"
              />
            </div>

            {/* Employee ID */}
            <div>
              <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, fontWeight: 600, color: 'var(--dim, #94a3b8)', marginBottom: 6 }}>
                <Hash size={13} style={{ color: 'var(--accent, #3b82f6)' }} />
                <span>Employee ID</span>
              </label>
              <input
                type="text"
                className="profile-input"
                value={employeeId}
                onChange={(e) => setEmployeeId(e.target.value)}
                placeholder="e.g. EMP-1042"
              />
            </div>

            {/* Department */}
            <div>
              <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, fontWeight: 600, color: 'var(--dim, #94a3b8)', marginBottom: 6 }}>
                <Building size={13} style={{ color: 'var(--accent, #3b82f6)' }} />
                <span>Department</span>
              </label>
              <input
                type="text"
                className="profile-input"
                value={department}
                onChange={(e) => setDepartment(e.target.value)}
                placeholder="e.g. Engineering / Product"
              />
            </div>

            {/* Designation */}
            <div>
              <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, fontWeight: 600, color: 'var(--dim, #94a3b8)', marginBottom: 6 }}>
                <Briefcase size={13} style={{ color: 'var(--accent, #3b82f6)' }} />
                <span>Designation</span>
              </label>
              <input
                type="text"
                className="profile-input"
                value={designation}
                onChange={(e) => setDesignation(e.target.value)}
                placeholder="e.g. Senior Product Manager"
              />
            </div>

            {/* Date of Birth */}
            <div>
              <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, fontWeight: 600, color: 'var(--dim, #94a3b8)', marginBottom: 6 }}>
                <Calendar size={13} style={{ color: 'var(--accent, #3b82f6)' }} />
                <span>Date of Birth</span>
              </label>
              <input
                type="date"
                className="profile-input"
                value={dob}
                onChange={(e) => setDob(e.target.value)}
              />
            </div>

            {/* Gender */}
            <div className="col-span-1 sm:col-span-2">
              <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, fontWeight: 600, color: 'var(--dim, #94a3b8)', marginBottom: 6 }}>
                <Heart size={13} style={{ color: 'var(--accent, #3b82f6)' }} />
                <span>Gender</span>
              </label>
              <select
                className="profile-input"
                value={gender}
                onChange={(e) => setGender(e.target.value)}
              >
                <option value="">Select gender...</option>
                <option value="Male">Male</option>
                <option value="Female">Female</option>
                <option value="Non-binary">Non-binary</option>
                <option value="Other">Other</option>
                <option value="Prefer not to say">Prefer not to say</option>
              </select>
            </div>
          </div>

          {/* Footer Actions */}
          <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 12, paddingTop: 16, marginTop: 4, borderTop: '1px solid var(--border-subtle, rgba(255,255,255,0.08))' }}>
            <button
              type="button"
              className="btn ghost"
              onClick={onClose}
              style={{ padding: '8px 18px', fontSize: 13, borderRadius: 8, cursor: 'pointer' }}
            >
              Cancel
            </button>
            <button
              type="submit"
              className="btn primary"
              disabled={isSaving}
              style={{ display: 'inline-flex', alignItems: 'center', gap: 8, padding: '8px 20px', fontSize: 13, borderRadius: 8, fontWeight: 600, cursor: 'pointer' }}
            >
              {isSaving ? (
                'Saving…'
              ) : (
                <>
                  <Check size={15} />
                  <span>Save Changes</span>
                </>
              )}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
};
