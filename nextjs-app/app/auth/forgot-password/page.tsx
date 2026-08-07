'use client'

import { useState } from 'react'
import { useRouter } from 'next/navigation'
import { KeyRound, ShieldCheck, ArrowLeft, Check } from 'lucide-react'
import { forgotPassword } from '@/lib/auth-client'

export default function ForgotPasswordPage() {
  const router = useRouter()
  const [email, setEmail] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [sent, setSent] = useState(false)

  const handleSubmit = async () => {
    if (!email.trim()) {
      setError('Please enter your email address.')
      return
    }

    setLoading(true)
    setError(null)

    try {
      await forgotPassword(email)
      setSent(true)
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Failed to send reset email.')
    } finally {
      setLoading(false)
    }
  }

  if (sent) {
    return (
      <div className="min-h-screen bg-base-100 flex items-center justify-center p-4">
        <div className="card bg-base-100 shadow-2xl border border-base-300 w-full max-w-md">
          <div className="card-body items-center text-center gap-6">
            <div className="inline-flex items-center justify-center w-16 h-16 rounded-full bg-success/10">
              <Check className="w-8 h-8 text-success" />
            </div>
            <div>
              <h2 className="text-2xl font-bold">Check Your Email</h2>
              <p className="text-sm text-base-content/60 mt-2">
                We&apos;ve sent a password reset link to <strong>{email}</strong>.
              </p>
            </div>
            <button onClick={() => router.push('/login')} className="btn btn-primary w-full">
              Back to Login
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
              <h2 className="text-2xl font-bold">Reset Password</h2>
              <p className="text-sm text-base-content/60 mt-1">
                Enter your email and we&apos;ll send you a reset link.
              </p>
            </div>

            {error ? (
              <div className="alert alert-error text-sm py-2">
                <span>{error}</span>
              </div>
            ) : null}

            <div className="space-y-4">
              <div className="form-control">
                <label className="label">
                  <span className="label-text font-medium">Email Address</span>
                </label>
                <input
                  type="email"
                  placeholder="name@email.com"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && handleSubmit()}
                  className="input input-bordered w-full"
                />
              </div>

              <button onClick={handleSubmit} disabled={loading} className="btn btn-primary w-full">
                {loading ? <span className="loading loading-spinner loading-sm"></span> : 'Send Reset Link'}
              </button>
            </div>

            <button
              onClick={() => router.push('/login')}
              className="btn btn-ghost btn-sm gap-2 w-full"
            >
              <ArrowLeft className="w-4 h-4" />
              Back to Login
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
