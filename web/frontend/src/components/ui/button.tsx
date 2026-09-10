import * as React from 'react'
import { Slot } from '@radix-ui/react-slot'
import { cva, type VariantProps } from 'class-variance-authority'
import { clsx } from 'clsx'
import { twMerge } from 'tailwind-merge'

const buttonVariants = cva('inline-flex items-center justify-center gap-2 rounded-xl text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-slate-500 disabled:pointer-events-none disabled:opacity-50 min-h-11 px-4', {
  variants: { variant: { default: 'bg-slate-900 text-white hover:bg-slate-700', outline: 'border border-slate-200 bg-white text-slate-800 hover:bg-slate-50' } }, defaultVariants: { variant: 'default' },
})
interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement>, VariantProps<typeof buttonVariants> { asChild?: boolean }
const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(({ className, variant, asChild = false, ...props }, ref) => {
  const Comp = asChild ? Slot : 'button'
  return <Comp className={twMerge(clsx(buttonVariants({ variant, className })))} ref={ref} {...props} />
})
Button.displayName = 'Button'
export { Button }
