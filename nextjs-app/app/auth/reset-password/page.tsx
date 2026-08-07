'use client'

import { useState } from 'react'
import { useRouter } from 'next/navigation'
import { KeyRound, ShieldCheck, Check } from 'lucide-react'
import { resetPassword } from '@/lib/auth-client'

export default function ResetPasswordPage() {
  const router = useRouter()
  const [newPassword, setNewPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [success, setSuccess] = useState(false)

  const handleReset = async () => {
    if (!newPassword.trim() || !confirmPassword.trim()) {
      setError('Please fill in both fields.')
      return
    }
    if (newPassword !== confirmPassword) {
      setError('Passwords do not match.')
      return
    }
    if (newPassword.length < 6) {
      setError('Password must be at least 6 characters.')
      return
    }

    setLoading(true)
    setError(null)

    try {
      await resetPassword(newPassword)
      setSuccess(true)
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Failed to reset password.')
    } finally {
      setLoading(false)
    }
  }

  if (success) {
    return (
      <div className="min-h-screen bg-base-100 flex items-center justify-center p-4">
        <div className="card bg-base-100 shadow-2xl border border-base-300 w-full max-w-md">
          <div className="card-body items-center text-center gap-6">
            <div className="inline-flex items-center justify-center w-16 h-16 rounded-full bg-success/10">
              <Check className="w-8 h-8 text-success" />
            </div>
            <div>
              <h2 className="text-2xl font-bold">Password Updated</h2>
              <p className="text-sm text-base-content/60 mt-2">
                Your password has been reset successfully.
              </p>
            </div>
            <button onClick={() => router.push('/login')} className="btn btn-primary w-full">
              Sign In
            </button>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="min-h-screen bg-base-100 flex flex-col items-center justify-center p-4">
      <div className="w-full max-w-md">
        <div className="text-center mb-8">
          <a href="/" className="inline-flex items-center gap-2 mb-6">
            <ShieldCheck className="w-8 h-8 text-primary" />
            <span className="text-xl font-extrabold tracking-tight text-base-content">SafeBill</span>
          </a>
        </div>

        <div className="card bg-base-100 shadow-2xl border border-base-300">
          <div className="card-body gap-6">
            <div className="text-center">
              <div className="inline-flex items-center justify-center w-16 h-16 rounded-full bg-primary/10 mb-4">
                <KeyRound className="w-8 h-8 text-primary" />
              </div>
              <h2 className="text-2xl font-bold">Set New Password</h2>
            </div>

            {error ? (
              <div className="alert alert-error text-sm py-2">
                <span>{error}</span>
              </div>
            ) : null}

            <div className="space-y-4">
              <div className="form-control">
                <label className="label">
                  <span className="label-text font-medium">New Password</span>
                </label>
                <input
                  type="password"
                  placeholder="Enter new password"
                  value={newPassword}
                  onChange={(e) => setNewPassword(e.target.value)}
                  className="input input-bordered w-full"
                />
              </div>

              <div className="form-control">
                <label className="label">
                  <span className="label-text font-medium">Confirm Password</span>
                </label>
                <input
                  type="password"
                  placeholder="Confirm new password"
                  value={confirmPassword}
                  onChange={(e) => setConfirmPassword(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && handleReset()}
                  className="input input-bordered w-full"
                />
              </div>

              <button onClick={handleReset} disabled={loading} className="btn btn-primary w-full">
                {loading ? <span className="loading loading-spinner loading-sm"></span> : 'Reset Password'}
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
