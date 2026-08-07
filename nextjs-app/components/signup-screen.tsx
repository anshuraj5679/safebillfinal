'use client'

import { useRef, useState } from 'react'
import { useRouter } from 'next/navigation'
import { Check, Copy, Mail, ShieldCheck, Smartphone, UserPlus } from 'lucide-react'

import { persistClientAuthCookies, signUpWithIdentifier } from '@/lib/auth-client'
import { useGsapReveal } from '@/lib/gsap-helpers'
import { useAuthStore } from '@/lib/store/auth-store'
import type { UserType } from '@/lib/types'


function accountLabel(userType: UserType): string {
  return userType === 'merchant' ? 'Merchant ID' : 'Consumer ID'
}

export function SignupScreen() {
  const router = useRouter()
  const { setAuth } = useAuthStore()
  const rootRef = useRef<HTMLDivElement>(null)

  const [userType, setUserType] = useState<UserType>('consumer')
  const [name, setName] = useState('')
  const [identifier, setIdentifier] = useState('')
  const [password, setPassword] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [copied, setCopied] = useState(false)
  const [createdCustomId, setCreatedCustomId] = useState<string | null>(null)
  const [signupSuccess, setSignupSuccess] = useState(false)
  const [needsVerification, setNeedsVerification] = useState(false)

  useGsapReveal(rootRef, [userType, createdCustomId, signupSuccess, copied, error, loading])

  const handleSignup = async () => {
    if (!name.trim() || !identifier.trim() || !password.trim()) {
      setError('Name, email or phone, and password are required.')
      return
    }

    setLoading(true)
    setError(null)

    try {
      const result = await signUpWithIdentifier({
        identifier: identifier.trim(),
        password,
        name: name.trim(),
        userType,
      })

      setCreatedCustomId(result.customId)

      if (result.needsVerification) {
        setNeedsVerification(true)
        return
      }

      // Auto-login successful
      await setAuth(result.user, result.token)
      persistClientAuthCookies(result.token, result.userType)
      setSignupSuccess(true)
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Signup failed.')
    } finally {
      setLoading(false)
    }
  }

  const copyId = () => {
    if (!createdCustomId) return
    navigator.clipboard.writeText(createdCustomId)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  // Success screen
  if (signupSuccess && createdCustomId) {
    return (
      <div ref={rootRef} className="min-h-screen bg-base-200 flex items-center justify-center p-4">
        <div data-gsap="card" className="card bg-base-100 shadow-2xl w-full max-w-md border border-base-300">
          <div className="card-body items-center text-center gap-6">
            <div data-gsap="hero" className="inline-flex items-center justify-center w-20 h-20 rounded-full bg-success/10">
              <Check className="w-10 h-10 text-success" />
            </div>
            <div data-gsap="hero">
              <h2 className="text-2xl font-bold">Account Ready</h2>
              <p className="text-sm text-base-content/60 mt-2">
                Your account is active. Save your {accountLabel(userType)} for future sign-in.
              </p>
            </div>

            <div className="w-full">
              <div data-gsap="card" className="flex items-center justify-center gap-3 p-4 bg-base-200 rounded-xl">
                <kbd className="kbd kbd-lg font-mono tracking-wider">{createdCustomId}</kbd>
                <button onClick={copyId} data-gsap-hover="lift" className="btn btn-ghost btn-sm btn-circle">
                  {copied ? <Check className="w-4 h-4 text-success" /> : <Copy className="w-4 h-4" />}
                </button>
              </div>
              <p className="mt-3 text-xs text-base-content/60">{identifier}</p>
              {copied ? <p className="text-success text-xs mt-2 font-medium">Copied to clipboard.</p> : null}
            </div>

            <button
              onClick={() => router.push(userType === 'merchant' ? '/merchant-dashboard' : '/locker')}
              data-gsap-hover="lift"
              className="btn btn-primary w-full"
            >
              Continue to {userType === 'merchant' ? 'Dashboard' : 'SafeBill'}
            </button>
          </div>
        </div>
      </div>
    )
  }

  // Verification required screen
  if (needsVerification && createdCustomId) {
    return (
      <div ref={rootRef} className="min-h-screen bg-base-200 flex items-center justify-center p-4">
        <div data-gsap="card" className="card bg-base-100 shadow-2xl w-full max-w-md border border-base-300">
          <div className="card-body gap-6">
            <div className="text-center">
              <div className="inline-flex items-center justify-center w-16 h-16 rounded-full bg-primary/10 mb-4">
                <Mail className="w-8 h-8 text-primary" />
              </div>
              <h2 className="text-2xl font-bold">Check Your Email</h2>
              <p className="text-sm text-base-content/60 mt-2">
                We&apos;ve sent a confirmation link to <strong>{identifier}</strong>. Click it to activate your account.
              </p>
            </div>

            <div className="rounded-2xl bg-base-200 p-4 text-center">
              <p className="text-xs uppercase tracking-[0.2em] text-base-content/50">{accountLabel(userType)}</p>
              <div className="mt-2 flex items-center justify-center gap-3">
                <kbd className="kbd kbd-lg font-mono tracking-wider">{createdCustomId}</kbd>
                <button onClick={copyId} className="btn btn-ghost btn-sm btn-circle">
                  {copied ? <Check className="w-4 h-4 text-success" /> : <Copy className="w-4 h-4" />}
                </button>
              </div>
              {copied ? <p className="text-success text-xs mt-2 font-medium">Copied to clipboard.</p> : null}
            </div>

            <button onClick={() => router.push('/login')} className="btn btn-primary w-full">
              Go to Login
            </button>
          </div>
        </div>
      </div>
    )
  }

  // Signup form
  const identifierLooksLikeEmail = identifier.includes('@')

  return (
    <div ref={rootRef} className="min-h-screen bg-base-200 flex flex-col">
      <div data-gsap="hero" className="navbar bg-base-100/80 backdrop-blur-md sticky top-0 z-50 border-b border-base-300">
        <div className="flex-1">
          <a className="btn btn-ghost text-xl gap-2 normal-case">
            <ShieldCheck className="w-6 h-6 text-primary" />
            <span className="font-bold">SafeBill</span>
          </a>
        </div>
        <div className="flex-none">
        </div>
      </div>

      <div className="flex-1 flex items-center justify-center p-4">
        <div data-gsap="card" className="card bg-base-100 shadow-2xl w-full max-w-md border border-base-300">
          <div className="card-body gap-6">
            <div data-gsap="hero" className="text-center">
              <div className="inline-flex items-center justify-center w-16 h-16 rounded-full bg-primary/10 mb-4">
                <UserPlus className="w-8 h-8 text-primary" />
              </div>
              <h1 className="text-2xl font-bold">Create Account</h1>
              <p className="text-sm text-base-content/60 mt-1">Register with email or phone and get a unique SafeBill ID</p>
            </div>

            <div data-gsap="card" className="tabs tabs-boxed bg-base-200 p-1">
              <button
                className={`tab flex-1 ${userType === 'consumer' ? 'tab-active' : ''}`}
                onClick={() => {
                  setUserType('consumer')
                  setError(null)
                }}
              >
                Consumer
              </button>
              <button
                className={`tab flex-1 ${userType === 'merchant' ? 'tab-active' : ''}`}
                onClick={() => {
                  setUserType('merchant')
                  setError(null)
                }}
              >
                Merchant
              </button>
            </div>

            {error ? (
              <div className="alert alert-error text-sm">
                <span>{error}</span>
              </div>
            ) : null}

            <div className="space-y-4">
              <div data-gsap="card" className="form-control">
                <label className="label">
                  <span className="label-text font-medium">Full Name</span>
                </label>
                <input
                  type="text"
                  placeholder="John Doe"
                  value={name}
                  onChange={(event) => setName(event.target.value)}
                  className="input input-bordered w-full"
                />
              </div>

              <div data-gsap="card" className="form-control">
                <label className="label">
                  <span className="label-text font-medium">Email or Mobile Number</span>
                </label>
                <label className="input input-bordered flex items-center gap-2">
                  {identifierLooksLikeEmail ? <Mail className="w-4 h-4 text-base-content/50" /> : <Smartphone className="w-4 h-4 text-base-content/50" />}
                  <input
                    type="text"
                    placeholder="name@gmail.com or +91 9876543210"
                    value={identifier}
                    onChange={(event) => setIdentifier(event.target.value)}
                    className="grow bg-transparent outline-none"
                  />
                </label>
                <p className="mt-2 text-xs text-base-content/50">
                  We will create a unique {accountLabel(userType)} tied to this account.
                </p>
              </div>

              <div data-gsap="card" className="form-control">
                <label className="label">
                  <span className="label-text font-medium">Password</span>
                </label>
                <input
                  type="password"
                  placeholder="Use a strong password"
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  onKeyDown={(event) => event.key === 'Enter' && handleSignup()}
                  className="input input-bordered w-full"
                />
              </div>

              <button onClick={handleSignup} disabled={loading} data-gsap-hover="lift" className="btn btn-primary w-full">
                {loading ? <span className="loading loading-spinner loading-sm"></span> : `Create ${accountLabel(userType)}`}
              </button>
            </div>

            <p className="text-center text-sm text-base-content/60">
              Already have an account?{' '}
              <button onClick={() => router.push('/login')} className="link link-primary font-semibold">
                Sign in
              </button>
            </p>
          </div>
        </div>
      </div>
    </div>
  )
}
