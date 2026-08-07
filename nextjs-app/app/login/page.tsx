'use client'

import { useState } from 'react'
import { useRouter } from 'next/navigation'
import { LogIn, ShieldCheck } from 'lucide-react'

import { persistClientAuthCookies, signInWithIdentifier } from '@/lib/auth-client'
import { useAuthStore } from '@/lib/store/auth-store'
import type { UserType } from '@/lib/types'

export default function LoginPage() {
  const router = useRouter()
  const { setAuth } = useAuthStore()
  const [userType, setUserType] = useState<UserType>('consumer')
  const [identifier, setIdentifier] = useState('')
  const [password, setPassword] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const handleLogin = async () => {
    if (!identifier.trim() || !password.trim()) {
      setError('Please enter your email/phone and password.')
      return
    }

    setLoading(true)
    setError(null)

    try {
      const result = await signInWithIdentifier({
        identifier,
        password,
        userType,
      })

      await setAuth(result.user, result.token)
      persistClientAuthCookies(result.token, result.userType)
      router.push(result.userType === 'merchant' ? '/merchant-dashboard' : '/locker')
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Login failed. Please try again.')
    } finally {
      setLoading(false)
    }
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
              <div className="inline-flex items-center justify-center w-16 h-16 rounded-full bg-primary/10 mb-4 ring-1 ring-primary/20">
                <LogIn className="w-8 h-8 text-primary" />
              </div>
              <h2 className="text-2xl font-bold">Welcome back</h2>
              <p className="text-sm text-base-content/60 mt-1">
                Sign in with your email or phone and password
              </p>
            </div>

            <div className="tabs tabs-boxed bg-base-200 p-1">
              <button
                className={`tab flex-1 transition-all duration-200 ${userType === 'consumer' ? 'tab-active font-semibold shadow-sm' : ''}`}
                onClick={() => {
                  setUserType('consumer')
                  setError(null)
                }}
              >
                Consumer
              </button>
              <button
                className={`tab flex-1 transition-all duration-200 ${userType === 'merchant' ? 'tab-active font-semibold shadow-sm' : ''}`}
                onClick={() => {
                  setUserType('merchant')
                  setError(null)
                }}
              >
                Merchant
              </button>
            </div>

            {error ? (
              <div className="alert alert-error text-sm py-2 rounded-lg shadow-sm">
                <span>{error}</span>
              </div>
            ) : null}

            <div className="space-y-4">
              <div className="form-control">
                <label className="label">
                  <span className="label-text font-medium text-base-content/80">
                    {userType === 'consumer' ? 'Email or phone' : 'Email or phone'}
                  </span>
                </label>
                <input
                  type="text"
                  placeholder="name@email.com or +91XXXXXXXXXX"
                  value={identifier}
                  onChange={(event) => setIdentifier(event.target.value)}
                  className="input input-bordered w-full focus:input-primary bg-base-50"
                  onKeyDown={(event) => event.key === 'Enter' && handleLogin()}
                />
              </div>

              <div className="form-control">
                <label className="label">
                  <span className="label-text font-medium text-base-content/80">Password</span>
                </label>
                <input
                  type="password"
                  placeholder="Enter your password"
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  onKeyDown={(event) => event.key === 'Enter' && handleLogin()}
                  className="input input-bordered w-full focus:input-primary bg-base-50"
                />
              </div>

              <div className="flex items-center justify-end text-sm">
                <button
                  onClick={() => router.push(`/auth/forgot-password?userType=${userType}`)}
                  className="link link-primary font-medium"
                >
                  Forgot password?
                </button>
              </div>

              <button
                onClick={handleLogin}
                disabled={loading}
                className="btn btn-primary w-full shadow-lg shadow-primary/20"
              >
                {loading ? <span className="loading loading-spinner loading-sm"></span> : 'Sign In'}
              </button>
            </div>

            <p className="text-center text-sm text-base-content/60">
              Don&apos;t have an account?{' '}
              <button
                onClick={() => router.push('/signup')}
                className="link link-primary font-semibold hover:underline"
              >
                Create one
              </button>
            </p>
          </div>
        </div>
      </div>
    </div>
  )
}
