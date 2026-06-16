export const metadata = {
  title: "RenderPR e2e — Next",
  description: "RenderPR Phase 3 end-to-end test fixture",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
